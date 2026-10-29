import os

import pytest
from fastapi.testclient import TestClient

import curator.setup as wizard
import curator.web as web
from curator.advisor import MockAdvisor
from curator.config import Settings, load_settings, save_env
from curator.confluence import ConfluenceError
from curator.db import Database
from curator.demo import FakeChat, FakeConfluence, FakeJira
from curator.service import Curator
from curator.web import create_app

LOCAL = ("127.0.0.1", 50000)
KEYS = [
    "CONFLUENCE_BASE_URL", "CONFLUENCE_EMAIL", "CONFLUENCE_API_TOKEN", "CONFLUENCE_SPACE_KEY", "CURATOR_ADVISOR",
    "JIRA_BASE_URL", "JIRA_EMAIL", "JIRA_API_TOKEN", "ANTHROPIC_API_KEY", "OLLAMA_URL", "OLLAMA_MODEL",
    "CURATOR_USER", "CURATOR_PASSWORD", "CURATOR_ENV_FILE", "CURATOR_DEMO", "ANTHROPIC_AUTH_TOKEN", "SLACK_TOKEN",
]


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Archivo de configuración temporal y entorno limpio, restaurado al terminar."""
    saved = {key: os.environ.get(key) for key in KEYS}
    for key in KEYS:
        os.environ.pop(key, None)
    path = tmp_path / "config.env"
    os.environ["CURATOR_ENV_FILE"] = str(path)
    yield path
    for key, value in saved.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


class SavedConfluence(FakeConfluence):
    """Confluence simulado que no se marca como demo, para probar el guardado."""

    demo = False


class FakeClient:
    """Reemplaza a ConfluenceClient en el asistente."""

    calls = []
    fail_with = None

    def __init__(self, base_url, email, token):
        FakeClient.calls.append((base_url, email, token))

    def list_spaces(self):
        if FakeClient.fail_with:
            raise FakeClient.fail_with
        return [("SIM", "Simulador VR"), ("QA", "Calidad")]


@pytest.fixture
def app(env, monkeypatch):
    FakeClient.calls, FakeClient.fail_with = [], None
    monkeypatch.setattr(wizard, "ConfluenceClient", FakeClient)
    # Al guardar, la app se reconstruye con el Confluence simulado en vez de conectarse de verdad.
    monkeypatch.setattr(
        web,
        "components",
        lambda s: dict(
            advisor=MockAdvisor(), confluence=SavedConfluence(), space_key=s.confluence_space_key, candidates=10,
            jira=FakeJira(), jira_max_images=5, pdf_max_scanned=5, chat=FakeChat() if s.slack_token else None,
        ),
    )
    settings = load_settings()
    curator = Curator(Database(":memory:"), MockAdvisor(), None, "", background=False)
    return create_app(curator, settings), curator, settings


def test_save_env_keeps_comments_and_other_keys(env):
    env.write_text("# Mi configuración\nOTRA=1\nCONFLUENCE_EMAIL=viejo@acme.com\nJIRA_EMAIL=x@acme.com\n")
    save_env({"CONFLUENCE_EMAIL": "nuevo@acme.com", "JIRA_EMAIL": "", "CONFLUENCE_SPACE_KEY": "SIM"})
    assert env.read_text() == "# Mi configuración\nOTRA=1\nCONFLUENCE_EMAIL=nuevo@acme.com\nCONFLUENCE_SPACE_KEY=SIM\n"
    assert os.environ["CONFLUENCE_EMAIL"] == "nuevo@acme.com" and "JIRA_EMAIL" not in os.environ
    assert oct(env.stat().st_mode & 0o777) == "0o600"


def test_first_run_redirects_to_wizard(app):
    application, _, _ = app
    page = TestClient(application, client=LOCAL).get("/")
    assert page.url.path == "/setup" and "Bienvenido" in page.text and "Probar conexión" in page.text


def test_wizard_only_from_this_computer(app):
    application, _, settings = app
    remote = TestClient(application, client=("192.168.1.20", 50000))
    assert remote.get("/setup").status_code == 403
    assert remote.post("/setup/test/confluence", data={}).status_code == 403
    assert remote.post("/setup", data={}).status_code == 403
    settings.setup_open = True
    assert remote.get("/setup").status_code == 200


def test_connection_tests(app, monkeypatch):
    application, _, _ = app
    client = TestClient(application, client=LOCAL)
    incomplete = client.post("/setup/test/confluence", data={"base_url": "acme.atlassian.net"}).json()
    assert not incomplete["ok"] and "Completa" in incomplete["message"]

    ok = client.post(
        "/setup/test/confluence", data={"base_url": "acme.atlassian.net/wiki/", "email": "yo@acme.com", "token": "t"}
    ).json()
    assert ok["ok"] and ok["spaces"] == [["SIM", "Simulador VR"], ["QA", "Calidad"]]
    assert FakeClient.calls[-1] == ("https://acme.atlassian.net", "yo@acme.com", "t")

    FakeClient.fail_with = ConfluenceError("Confluence respondió 401: Unauthorized", 401)
    bad = client.post("/setup/test/confluence", data={"base_url": "acme.atlassian.net", "email": "yo@acme.com", "token": "x"}).json()
    assert not bad["ok"] and "rechazó el correo o el token" in bad["message"]

    monkeypatch.setattr(wizard, "JiraClient", lambda url, email, token: type("J", (), {"myself": lambda self: "Ana Pérez"})())
    jira = client.post("/setup/test/jira", data={"base_url": "acme.atlassian.net", "email": "yo@acme.com", "token": "t"}).json()
    assert jira == {"ok": True, "message": "Conexión correcta como Ana Pérez."}

    from curator.advisor import OllamaAdvisor

    monkeypatch.setattr(OllamaAdvisor, "models", lambda self: ["gemma3:12b", "qwen2.5vl:7b"])
    assert client.post("/setup/test/ollama", data={"url": ""}).json()["models"] == ["gemma3:12b", "qwen2.5vl:7b"]
    monkeypatch.setattr(OllamaAdvisor, "models", lambda self: [])
    assert "ollama pull" in client.post("/setup/test/ollama", data={}).json()["message"]

    from curator.advisor import AnthropicAdvisor

    monkeypatch.setattr(AnthropicAdvisor, "check", lambda self: "Claude Opus 5.5")
    assert client.post("/setup/test/anthropic", data={"api_key": "sk-ant-x"}).json()["ok"]
    assert "Pega la API key" in client.post("/setup/test/anthropic", data={}).json()["message"]


def test_save_configures_and_indexes(app, env):
    application, curator, settings = app
    client = TestClient(application, client=LOCAL)
    form = {
        "confluence_base_url": "acme.atlassian.net",
        "confluence_email": "yo@acme.com",
        "confluence_api_token": "token-1",
        "confluence_space_key": "sim",
        "same_jira": "1",
        "advisor": "ollama",
        "ollama_url": "http://gpu.local:11434/",
        "ollama_model": "gemma3:27b",
    }
    saved = client.post("/setup", data=form)
    assert saved.url.path == "/" and "Se indexaron 4 páginas del espacio SIM" in saved.text
    text = env.read_text()
    assert "CONFLUENCE_BASE_URL=https://acme.atlassian.net" in text and "CONFLUENCE_SPACE_KEY=SIM" in text
    assert "OLLAMA_URL=http://gpu.local:11434" in text and "JIRA_" not in text
    assert settings.confluence_configured and settings.jira_base_url == "https://acme.atlassian.net"
    assert settings.advisor == "ollama" and curator.confluence is not None and curator.space_key == "SIM"
    assert len(curator.pages()) == 4

    # Al volver, los secretos no se muestran y un campo vacío conserva el guardado.
    page = client.get("/setup").text
    assert "token-1" not in page and "Guardado. Déjalo vacío para conservarlo" in page
    client.post("/setup", data={**form, "confluence_api_token": "", "advisor": "mock"})
    assert "CONFLUENCE_API_TOKEN=token-1" in env.read_text() and settings.advisor == "mock"


def test_password_can_be_set_and_removed(app, env):
    application, _, settings = app
    client = TestClient(application, client=LOCAL)
    base = {
        "confluence_base_url": "acme.atlassian.net", "confluence_email": "yo@acme.com",
        "confluence_api_token": "t", "confluence_space_key": "SIM", "same_jira": "1", "advisor": "mock",
    }
    client.post("/setup", data={**base, "app_user": "equipo", "app_password": "secreta"})
    assert settings.app_password == "secreta"
    assert client.get("/").status_code == 401  # la contraseña aplica de inmediato
    assert client.get("/", auth=("equipo", "secreta")).status_code == 200
    client.post("/setup", data={**base, "remove_password": "1"}, auth=("equipo", "secreta"))
    assert not settings.app_password and "CURATOR_PASSWORD" not in env.read_text()


def test_validation_messages(app):
    application, _, _ = app
    client = TestClient(application, client=LOCAL)
    missing = client.post("/setup", data={"confluence_email": "yo@acme.com", "advisor": "mock"})
    assert "Falta la dirección de Confluence, el token, el espacio." in missing.text
    no_key = client.post(
        "/setup",
        data={"confluence_base_url": "a.atlassian.net", "confluence_email": "e", "confluence_api_token": "t",
              "confluence_space_key": "S", "advisor": "anthropic"},
    )
    assert "falta la API key" in no_key.text
    half = client.post(
        "/setup",
        data={"confluence_base_url": "a.atlassian.net", "confluence_email": "e", "confluence_api_token": "t",
              "confluence_space_key": "S", "advisor": "mock", "app_user": "equipo"},
    )
    assert "completa el usuario y la contraseña" in half.text


def test_demo_does_not_save(env):
    curator = Curator(Database(":memory:"), MockAdvisor(), FakeConfluence(), "DEMO", background=False)
    client = TestClient(create_app(curator, Settings()), client=LOCAL)
    assert "modo demo" in client.get("/setup").text
    response = client.post(
        "/setup",
        data={"confluence_base_url": "a.atlassian.net", "confluence_email": "e", "confluence_api_token": "t",
              "confluence_space_key": "S", "advisor": "mock"},
    )
    assert "no se guardan" in response.text and not env.exists()


def test_slack_step(app, env, monkeypatch):
    application, curator, settings = app
    client = TestClient(application, client=LOCAL)

    class FakeSlack:
        def __init__(self, token):
            self.token = token

        def check(self):
            if self.token != "xoxp-ok":
                from curator.chat import ChatError

                raise ChatError("Slack rechazó el token. Revisa que esté copiado completo.")
            return "Simulador VR como ana"

        def channels(self):
            return [object(), object()]

    monkeypatch.setattr("curator.chat.SlackSource", FakeSlack)
    assert client.post("/setup/test/slack", data={"token": "xoxp-ok"}).json() == {
        "ok": True, "message": "Conectado a Simulador VR como ana: 2 canales disponibles."
    }
    assert "rechazó el token" in client.post("/setup/test/slack", data={"token": "malo"}).json()["message"]

    base = {
        "confluence_base_url": "acme.atlassian.net", "confluence_email": "yo@acme.com",
        "confluence_api_token": "t", "confluence_space_key": "SIM", "same_jira": "1", "advisor": "mock",
    }
    assert "falta el token" in client.post("/setup", data={**base, "chat_source": "slack"}).text
    client.post("/setup", data={**base, "chat_source": "slack", "slack_token": "xoxp-ok"})
    assert "SLACK_TOKEN=xoxp-ok" in env.read_text() and curator.chat is not None
    assert "xoxp-ok" not in client.get("/setup").text
    client.post("/setup", data={**base, "chat_source": "slack"})  # vacío: conserva el guardado
    assert "SLACK_TOKEN=xoxp-ok" in env.read_text()
    client.post("/setup", data={**base, "chat_source": ""})
    assert "SLACK_TOKEN" not in env.read_text() and curator.chat is None
