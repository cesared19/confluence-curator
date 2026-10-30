"""Asistente de configuración: guía la primera puesta en marcha sin editar archivos a mano."""

import dataclasses

import httpx
from fastapi import FastAPI, Form, Request
from fastapi.responses import JSONResponse

from .config import env_path, load_settings, save_env
from .confluence import ConfluenceClient, ConfluenceError
from .jira import JiraClient

LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost"}
SECRET_KEYS = {"CONFLUENCE_API_TOKEN", "JIRA_API_TOKEN", "ANTHROPIC_API_KEY", "CURATOR_PASSWORD"}


def friendly(error: Exception, service: str) -> str:
    """Mensaje comprensible para errores de conexión frecuentes."""
    status = getattr(error, "status", None) or getattr(error, "status_code", None)
    if status == 401:
        return f"{service} rechazó el correo o el token. Revisa que el token sea de esa cuenta y que esté copiado completo."
    if status == 403:
        return f"La cuenta no tiene permiso en {service}. Pide acceso a quien administra el sitio."
    if status == 404:
        return f"No se encontró {service} en esa dirección. Revisa la URL (debe ser como https://tu-empresa.atlassian.net)."
    if isinstance(error, httpx.HTTPError):
        return f"No se pudo conectar con {service}. Revisa la URL y la conexión a internet. ({error})"
    return str(error)


def normalize_url(value: str) -> str:
    url = value.strip().rstrip("/")
    if url and not url.startswith(("http://", "https://")):
        url = "https://" + url
    return url.removesuffix("/wiki")


def settings_key(name: str) -> str:
    """Valor actual de una variable que no está en Settings (la API key de Anthropic la lee el SDK)."""
    import os

    return os.environ.get(name, "")


def is_allowed(request: Request, settings) -> bool:
    """Ajustes y actualizaciones tocan credenciales y archivos: se abren desde este equipo, con contraseña o si se
    habilitó a propósito."""
    host = request.client.host if request.client else ""
    return host in LOCAL_HOSTS or bool(settings.app_user and settings.app_password) or settings.setup_open


def register(app: FastAPI, curator, settings, render, back, rebuild) -> None:
    def allowed(request: Request) -> bool:
        return is_allowed(request, settings)

    def forbidden(request: Request):
        return render(request, "setup_forbidden.html", status_code=403)

    def secret(form_value: str, current: str) -> str:
        return form_value.strip() or current

    @app.get("/setup")
    def setup_page(request: Request, error: str = "", message: str = ""):
        if not allowed(request):
            return forbidden(request)
        return render(
            request,
            "setup.html",
            s=settings,
            same_jira=not any(
                [
                    settings.jira_base_url and settings.jira_base_url != settings.confluence_base_url,
                    settings.jira_email and settings.jira_email != settings.confluence_email,
                    settings.jira_api_token and settings.jira_api_token != settings.confluence_api_token,
                ]
            ),
            env_file=str(env_path().resolve()),
            has_anthropic_key=bool(settings_key("ANTHROPIC_API_KEY")),
            first_run=not settings.confluence_configured,
            demo=getattr(curator.confluence, "demo", False),
            error=error,
            message=message,
        )

    @app.post("/setup/test/confluence")
    def test_confluence(request: Request, base_url: str = Form(""), email: str = Form(""), token: str = Form("")):
        if not allowed(request):
            return JSONResponse({"ok": False, "message": "No permitido."}, status_code=403)
        url, token = normalize_url(base_url), secret(token, settings.confluence_api_token)
        if not (url and email.strip() and token):
            return {"ok": False, "message": "Completa la dirección, el correo y el token."}
        try:
            spaces = ConfluenceClient(url, email.strip(), token).list_spaces()
        except (ConfluenceError, httpx.HTTPError) as error:
            return {"ok": False, "message": friendly(error, "Confluence")}
        if not spaces:
            return {"ok": False, "message": "La conexión funciona, pero la cuenta no ve ningún espacio."}
        return {"ok": True, "message": f"Conexión correcta: {len(spaces)} espacios disponibles.", "spaces": spaces}

    @app.post("/setup/test/jira")
    def test_jira(request: Request, base_url: str = Form(""), email: str = Form(""), token: str = Form("")):
        if not allowed(request):
            return JSONResponse({"ok": False, "message": "No permitido."}, status_code=403)
        url = normalize_url(base_url)
        token = secret(token, settings.jira_api_token or settings.confluence_api_token)
        if not (url and email.strip() and token):
            return {"ok": False, "message": "Completa la dirección, el correo y el token."}
        try:
            name = JiraClient(url, email.strip(), token).myself()
        except (ConfluenceError, httpx.HTTPError) as error:
            return {"ok": False, "message": friendly(error, "Jira")}
        return {"ok": True, "message": f"Conexión correcta como {name}."}

    @app.post("/setup/test/anthropic")
    def test_anthropic(request: Request, api_key: str = Form(""), model: str = Form("")):
        if not allowed(request):
            return JSONResponse({"ok": False, "message": "No permitido."}, status_code=403)
        import anthropic

        from .advisor import AnthropicAdvisor

        key = secret(api_key, settings_key("ANTHROPIC_API_KEY"))
        if not key:
            return {"ok": False, "message": "Pega la API key."}
        model = model.strip() or settings.anthropic_model
        try:
            name = AnthropicAdvisor(model, settings.anthropic_effort, client=anthropic.Anthropic(api_key=key)).check()
        except anthropic.AuthenticationError:
            return {"ok": False, "message": "La API key no es válida."}
        except anthropic.PermissionDeniedError:
            return {"ok": False, "message": "La API key no tiene permiso para usar la API."}
        except anthropic.NotFoundError:
            return {"ok": False, "message": f"El modelo {model} no está disponible para esta cuenta."}
        except anthropic.APIConnectionError:
            return {"ok": False, "message": "No se pudo conectar con la API de Anthropic. Revisa la conexión a internet."}
        except anthropic.APIStatusError as error:
            return {"ok": False, "message": f"La API respondió {error.status_code}: {error.message}"}
        return {"ok": True, "message": f"API key correcta; modelo {name} disponible."}

    @app.post("/setup/test/slack")
    def test_slack(request: Request, token: str = Form("")):
        if not allowed(request):
            return JSONResponse({"ok": False, "message": "No permitido."}, status_code=403)
        from .chat import ChatError, SlackSource

        token = secret(token, settings.slack_token)
        if not token:
            return {"ok": False, "message": "Pega el token de Slack."}
        source = SlackSource(token)
        try:
            who = source.check()
            channels = source.channels()
        except ChatError as error:
            return {"ok": False, "message": str(error)}
        if not channels:
            return {"ok": True, "message": f"Conectado a {who}, pero todavía no ve ningún canal: invita la app con /invite."}
        return {"ok": True, "message": f"Conectado a {who}: {len(channels)} canales disponibles."}

    @app.post("/setup/test/ollama")
    def test_ollama(request: Request, url: str = Form("")):
        if not allowed(request):
            return JSONResponse({"ok": False, "message": "No permitido."}, status_code=403)
        from .advisor import OllamaAdvisor

        address = url.strip().rstrip("/") or "http://localhost:11434"
        try:
            models = OllamaAdvisor(address, "", 1024).models()
        except RuntimeError as error:
            return {"ok": False, "message": f"{error}. ¿Está Ollama abierto en ese equipo?"}
        if not models:
            return {"ok": False, "message": "Ollama responde, pero no tiene modelos. En una terminal: ollama pull gemma3:12b"}
        return {"ok": True, "message": f"Ollama responde: {len(models)} modelos descargados.", "models": models}

    @app.post("/setup")
    def save_setup(
        request: Request,
        confluence_base_url: str = Form(""),
        confluence_email: str = Form(""),
        confluence_api_token: str = Form(""),
        confluence_space_key: str = Form(""),
        same_jira: str = Form(""),
        jira_base_url: str = Form(""),
        jira_email: str = Form(""),
        jira_api_token: str = Form(""),
        advisor: str = Form("mock"),
        anthropic_api_key: str = Form(""),
        ollama_url: str = Form(""),
        ollama_model: str = Form(""),
        app_user: str = Form(""),
        app_password: str = Form(""),
        chat_source: str = Form(""),
        slack_token: str = Form(""),
        remove_password: str = Form(""),
    ):
        if not allowed(request):
            return forbidden(request)
        if getattr(curator.confluence, "demo", False):
            return back("/setup", error="En el modo demo los ajustes no se guardan. Cierra el demo y abre la herramienta normal.")
        if advisor not in ("mock", "anthropic", "ollama"):
            advisor = "mock"
        values = {
            "CONFLUENCE_BASE_URL": normalize_url(confluence_base_url),
            "CONFLUENCE_EMAIL": confluence_email.strip(),
            "CONFLUENCE_API_TOKEN": secret(confluence_api_token, settings.confluence_api_token),
            "CONFLUENCE_SPACE_KEY": confluence_space_key.strip().upper(),
            "CURATOR_ADVISOR": advisor,
        }
        if same_jira:
            values.update(JIRA_BASE_URL="", JIRA_EMAIL="", JIRA_API_TOKEN="")
        else:
            values.update(
                JIRA_BASE_URL=normalize_url(jira_base_url),
                JIRA_EMAIL=jira_email.strip(),
                JIRA_API_TOKEN=secret(jira_api_token, settings.jira_api_token),
            )
        values["SLACK_TOKEN"] = secret(slack_token, settings.slack_token) if chat_source == "slack" else ""
        if chat_source == "slack" and not values["SLACK_TOKEN"]:
            return back("/setup", error="Para usar Slack falta el token.")
        if advisor == "anthropic":
            values["ANTHROPIC_API_KEY"] = secret(anthropic_api_key, settings_key("ANTHROPIC_API_KEY"))
            if not values["ANTHROPIC_API_KEY"]:
                return back("/setup", error="Para usar Claude falta la API key.")
        if advisor == "ollama":
            values["OLLAMA_URL"] = ollama_url.strip().rstrip("/") or "http://localhost:11434"
            values["OLLAMA_MODEL"] = ollama_model.strip() or settings.ollama_model
        if remove_password:
            values.update(CURATOR_USER="", CURATOR_PASSWORD="")
        elif app_user.strip() or app_password.strip():
            values["CURATOR_USER"] = app_user.strip() or settings.app_user
            values["CURATOR_PASSWORD"] = secret(app_password, settings.app_password)
            if not (values["CURATOR_USER"] and values["CURATOR_PASSWORD"]):
                return back("/setup", error="Para pedir contraseña completa el usuario y la contraseña.")

        missing = [label for key, label in [
            ("CONFLUENCE_BASE_URL", "la dirección de Confluence"),
            ("CONFLUENCE_EMAIL", "el correo"),
            ("CONFLUENCE_API_TOKEN", "el token"),
            ("CONFLUENCE_SPACE_KEY", "el espacio"),
        ] if not values[key]]
        if missing:
            return back("/setup", error="Falta " + ", ".join(missing) + ".")

        try:
            save_env(values)
        except OSError as error:
            return back("/setup", error=f"No se pudo guardar la configuración en {env_path()}: {error}")
        fresh = load_settings()
        for field in dataclasses.fields(settings):
            setattr(settings, field.name, getattr(fresh, field.name))
        rebuild()
        try:
            count = curator.sync()
        except Exception as error:
            return back("/setup", error=f"La configuración se guardó, pero no se pudo leer el espacio: {friendly(error, 'Confluence')}")
        return back("/", message=f"Configuración guardada. Se indexaron {count} páginas del espacio {settings.confluence_space_key}.")
