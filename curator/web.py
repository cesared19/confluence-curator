import base64
import difflib
import hashlib
import html
import secrets
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import __version__, setup, storage, update_routes
from .advisor import AnthropicAdvisor, MockAdvisor, OllamaAdvisor
from .audit import KINDS
from .config import Settings, load_settings
from .db import Database, loads
from .service import Curator, CuratorError

HERE = Path(__file__).parent
SOURCES = ["Chat", "Jira", "Manual del producto", "Reunión", "Otro"]
ACTIONS = {
    "update_section": "Actualizar sección",
    "append_to_page": "Agregar al final de la página",
    "create_page": "Crear página nueva",
}
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_PDF_BYTES = 50 * 1024 * 1024
IMAGE_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}


def components(settings: Settings) -> dict:
    """Clientes de Confluence y Jira y el asesor según la configuración."""
    import os

    jira = chat = None
    if os.environ.get("CURATOR_DEMO") == "1":
        from .demo import FakeChat, FakeConfluence, FakeJira

        confluence, space_key, jira = FakeConfluence(), "DEMO", FakeJira()
        chat = FakeChat()
    elif settings.confluence_configured:
        from .confluence import ConfluenceClient

        confluence = ConfluenceClient(settings.confluence_base_url, settings.confluence_email, settings.confluence_api_token)
        space_key = settings.confluence_space_key
    else:
        confluence, space_key = None, ""
    if jira is None and settings.jira_configured:
        from .jira import JiraClient

        jira = JiraClient(settings.jira_base_url, settings.jira_email, settings.jira_api_token)
    if chat is None and settings.slack_token:
        from .chat import SlackSource

        chat = SlackSource(settings.slack_token)
    if settings.advisor == "anthropic":
        advisor = AnthropicAdvisor(settings.anthropic_model, settings.anthropic_effort)
    elif settings.advisor == "ollama":
        advisor = OllamaAdvisor(settings.ollama_url, settings.ollama_model, settings.ollama_context)
    else:
        advisor = MockAdvisor()
    return dict(
        advisor=advisor,
        confluence=confluence,
        space_key=space_key,
        candidates=settings.candidates,
        jira=jira,
        jira_max_images=settings.jira_max_images,
        pdf_max_scanned=settings.pdf_max_scanned,
        chat=chat,
    )


def build_curator(settings: Settings) -> Curator:
    parts = components(settings)
    curator = Curator(
        Database(settings.database_path),
        parts.pop("advisor"),
        parts.pop("confluence"),
        parts.pop("space_key"),
        parts.pop("candidates"),
        **parts,
    )
    curator.resume_pending()
    curator.start_watcher()
    return curator


def plural(count: int, one: str, many: str) -> str:
    return f"{count} {one if count == 1 else many}"


def imported(count: int, one: str, many: str, errors: list[str], feminine: bool = False) -> str:
    """Resultado de una importación para la actividad."""
    participle = ("importada" if feminine else "importado") + ("" if count == 1 else "s")
    text = f"{plural(count, one, many)} {participle}"
    text = text[0].upper() + text[1:] + ("." if not count else "; se analiza en cola." if count == 1 else "; se analizan en cola.")
    if errors:
        text += " Con problemas: " + " · ".join(errors)
    return text


def when(value: str) -> str:
    """Fecha ISO guardada en UTC, en formato corto."""
    try:
        return datetime.fromisoformat(value).astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    except (TypeError, ValueError):
        return value or ""


def diff_table(old: str, new: str) -> str:
    table = difflib.HtmlDiff(wrapcolumn=70).make_table(
        storage.pretty(old), storage.pretty(new), "Actual", "Propuesto", context=True, numlines=2
    )
    return table.replace('nowrap="nowrap"', "")


def preview(storage_html: str, note_id: int) -> str:
    """Documento para un iframe aislado (sandbox) con el contenido propuesto y las imágenes de la nota."""
    body = storage.replace_placeholders(
        storage_html, lambda n: f'<img src="/notes/{note_id}/images/{n}" style="max-width:100%">'
    )
    style = (
        "<style>body{font:15px/1.5 system-ui,sans-serif;margin:12px;color:#172b4d;background:#fff}"
        "table{border-collapse:collapse}td,th{border:1px solid #ccc;padding:4px 8px}"
        "@media (prefers-color-scheme:dark){body{color:#dfe1e6;background:#1d2125}}</style>"
    )
    return f"<!doctype html><meta charset=utf-8>{style}{body}"


def create_app(curator: Curator | None = None, settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    curator = curator or build_curator(settings)
    app = FastAPI(title="Confluence Curator")
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    templates = Jinja2Templates(directory=HERE / "templates")
    # La hoja de estilos se pide con su huella, para que el navegador no use una vieja después de actualizar.
    asset_version = hashlib.sha1((HERE / "static" / "style.css").read_bytes()).hexdigest()[:10]
    templates.env.globals.update(
        ACTIONS=ACTIONS, curator=curator, settings=settings, loads=loads, version=__version__, asset_version=asset_version
    )
    templates.env.filters["when"] = when

    def activity_state() -> dict:
        state = curator.activity.snapshot()
        state.update(queued=curator.queued(), paused=curator.paused)
        state["busy"] = bool(state["tasks"] or state["queued"])
        return state

    templates.env.globals["activity_state"] = activity_state

    @app.middleware("http")
    async def basic_auth(request: Request, call_next):
        # Se evalúa en cada petición: el asistente de configuración puede activar o cambiar la contraseña.
        if not (settings.app_user and settings.app_password):
            return await call_next(request)
        header = request.headers.get("authorization", "")
        if header.startswith("Basic "):
            try:
                user, _, password = base64.b64decode(header[6:]).decode().partition(":")
            except ValueError:
                user, password = "", ""
            if secrets.compare_digest(user, settings.app_user) and secrets.compare_digest(password, settings.app_password):
                return await call_next(request)
        return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="curator"'})

    def render(request: Request, name: str, status_code: int = 200, **context):
        return templates.TemplateResponse(request, name, context, status_code=status_code)

    def back(url: str, error: str = "", message: str = "") -> RedirectResponse:
        params = "&".join(f"{k}={quote(v)}" for k, v in [("error", error), ("message", message)] if v)
        return RedirectResponse(f"{url}?{params}" if params else url, status_code=303)

    def rebuild() -> None:
        curator.reconfigure(**components(settings))

    setup.register(app, curator, settings, render, back, rebuild)
    update_routes.register(app, curator, settings, templates, render, back)

    @app.get("/", response_class=HTMLResponse)
    def inbox(request: Request, error: str = "", message: str = ""):
        if curator.confluence is None and not settings.confluence_configured:
            return RedirectResponse("/setup", status_code=303)  # primera vez: el asistente de configuración
        return render(
            request,
            "inbox.html",
            notes=curator.recent_notes(),
            sources=SOURCES,
            pages=len(curator.pages()),
            last_sync=curator.db.get_meta("last_sync"),
            watch=curator.watch_settings(),
            open_findings=curator.auditor.counts().get("open", 0),
            error=error,
            message=message,
        )

    @app.post("/notes")
    async def create_note(
        request: Request,
        text: str = Form(""),
        source_type: str = Form("Otro"),
        source_ref: str = Form(""),
        images: list[UploadFile] | None = None,
    ):
        files = []
        for upload in images or []:
            if not upload.filename:
                continue
            data = await upload.read()
            if upload.content_type not in IMAGE_TYPES:
                return back("/", error=f"{upload.filename}: solo se aceptan imágenes PNG, JPEG, GIF o WebP.")
            if len(data) > MAX_IMAGE_BYTES:
                return back("/", error=f"{upload.filename}: la imagen supera 10 MB.")
            files.append((upload.filename, upload.content_type, data))
        try:
            note_id = curator.create_note(text, source_type, source_ref, files)
        except CuratorError as error:
            return back("/", error=str(error))
        curator.enqueue(note_id)
        return back(f"/notes/{note_id}")

    @app.get("/notes/{note_id}", response_class=HTMLResponse)
    def note_page(request: Request, note_id: int, error: str = "", message: str = ""):
        try:
            note = curator.note(note_id)
        except CuratorError:
            return render(request, "missing.html", status_code=404)
        proposals = []
        for p in curator.proposals(note_id):
            page = curator.page(p["page_id"]) if p["page_id"] else None
            section = curator.section(p["page_id"], p["section_index"]) if p["action"] == "update_section" else None
            proposals.append(
                {
                    "row": p,
                    "page": page,
                    "heading": section.heading if section else "",
                    # Si la IA no dio una sección válida, se ofrecen las secciones reales de la página para elegir.
                    "sections": storage.split_sections(page.body) if page and p["error"] and p["status"] == "pending" else [],
                    "conflicts": loads(p["conflicts"]),
                    "diff": diff_table(p["old_storage"], p["new_storage"]),
                    "preview": preview(p["new_storage"], note_id),
                }
            )
        return render(
            request,
            "note.html",
            note=note,
            images=curator.note_images(note_id),
            proposals=proposals,
            alternatives=curator.alternatives(note),
            pages=curator.pages(),
            error=error,
            message=message,
        )

    @app.get("/notes/{note_id}/images/{index}")
    def note_image(note_id: int, index: int):
        for row in curator.note_images(note_id):
            if row["idx"] == index:
                return Response(row["data"], media_type=row["mime"])
        return Response(status_code=404)

    @app.post("/notes/{note_id}/reanalyze")
    def reanalyze(note_id: int, hint: str = Form(""), target: str = Form("")):
        page_id, _, section = target.partition("#")
        if page_id and curator.page(page_id) is None:
            return back(f"/notes/{note_id}", error="La página indicada no está en el índice.")
        curator.enqueue(note_id, hint=hint, target_page=page_id, target_section=int(section) if section else -1)
        return back(f"/notes/{note_id}")

    @app.post("/proposals/{proposal_id}/apply")
    def apply(proposal_id: int, content: str = Form(...), new_title: str = Form("")):
        try:
            note_id = curator.proposal(proposal_id)["note_id"]
            curator.apply(proposal_id, content, new_title)
        except (CuratorError, ValueError) as error:
            row = curator.db.one("SELECT note_id FROM proposals WHERE id = ?", (proposal_id,))
            return back(f"/notes/{row['note_id']}" if row else "/", error=str(error))
        return back(f"/notes/{note_id}", message="Cambio publicado en Confluence.")

    @app.post("/proposals/{proposal_id}/reject")
    def reject(proposal_id: int):
        try:
            note_id = curator.proposal(proposal_id)["note_id"]
        except CuratorError as error:
            return back("/", error=str(error))
        curator.reject(proposal_id)
        return back(f"/notes/{note_id}", message="Propuesta descartada.")

    @app.get("/jira", response_class=HTMLResponse)
    def jira_page(request: Request, q: str = "", error: str = "", message: str = ""):
        results = []
        if q.strip():
            try:
                results = curator.jira_search(q)
            except CuratorError as failure:
                error = str(failure)
        return render(
            request,
            "jira.html",
            q=q or curator.db.get_meta("last_jql"),
            searched=bool(q.strip()),
            results=results,
            watch=curator.watch_settings(),
            error=error,
            message=message,
        )

    @app.post("/jira/import")
    def jira_import(keys: list[str] = Form(default_factory=list)):
        if not keys:
            return back("/jira", error="Selecciona al menos un ticket.")
        if curator.jira is None:
            return back("/jira", error="Jira no está configurado.")

        def work(task):
            note_ids, errors = curator.import_issues(keys, task)
            return imported(len(note_ids), "ticket", "tickets", errors)

        curator.start_job("import", f"Importando {plural(len(keys), 'ticket', 'tickets')} de Jira", work)
        return back("/", message=f"Importando {plural(len(keys), 'ticket', 'tickets')} de Jira. El avance se ve en el bloque «Actividad».")

    @app.post("/jira/watch")
    def jira_watch(jql: str = Form(""), interval: int = Form(60), enabled: str = Form("")):
        try:
            curator.save_watch(jql, interval, enabled == "1")
        except CuratorError as error:
            return back("/jira", error=str(error))
        return back("/jira", message="Revisión automática activada." if enabled == "1" else "Revisión automática guardada (desactivada).")

    @app.post("/jira/watch/run")
    def jira_watch_run():
        curator.start_job(
            "watch", "Revisión de Jira", lambda task: "Revisión de Jira: " + curator.run_watch(task), "/jira#automatica"
        )
        return back("/jira", message="Revisando Jira. El avance se ve en el bloque «Actividad».")

    @app.get("/chat", response_class=HTMLResponse)
    def chat_page(request: Request, channel: str = "", days: int = 7, error: str = "", message: str = ""):
        channels, results = [], []
        if curator.chat is not None:
            try:
                channels = curator.chat_channels()
                if channel:
                    results = curator.chat_conversations(channel, days)
            except CuratorError as failure:
                error = str(failure)
        return render(
            request, "chat.html", channels=channels, channel=channel, days=days, results=results, error=error, message=message
        )

    @app.post("/chat/import")
    def chat_import(keys: list[str] = Form(default_factory=list), channel: str = Form(""), days: int = Form(7)):
        here = f"/chat?channel={quote(channel)}&days={days}"
        if not keys:
            return RedirectResponse(here + "&error=" + quote("Selecciona al menos una conversación."), status_code=303)
        if curator.chat is None:
            return RedirectResponse(here + "&error=" + quote("El chat no está configurado."), status_code=303)

        def work(task):
            note_ids, errors = curator.import_chat(keys, channel, days, task)
            return imported(len(note_ids), "conversación", "conversaciones", errors, feminine=True)

        label = f"Importando {plural(len(keys), 'conversación', 'conversaciones')} del chat"
        curator.start_job("import", label, work)
        return back("/", message=f"{label}. El avance se ve en el bloque «Actividad».")

    @app.get("/pdf", response_class=HTMLResponse)
    def pdf_list(request: Request, error: str = "", message: str = ""):
        return render(request, "pdfs.html", documents=curator.pdf_documents(), sources=SOURCES, error=error, message=message)

    @app.post("/pdf")
    async def pdf_upload(file: UploadFile | None = None, source_type: str = Form("Manual del producto")):
        if file is None or not file.filename:
            return back("/pdf", error="Elige un archivo PDF.")
        data = await file.read()
        if len(data) > MAX_PDF_BYTES:
            return back("/pdf", error=f"{file.filename}: el PDF supera 50 MB.")
        if not data.startswith(b"%PDF"):
            return back("/pdf", error=f"{file.filename}: no es un PDF.")
        try:
            document_id = curator.add_pdf(file.filename, data, source_type)
        except CuratorError as error:
            return back("/pdf", error=str(error))
        return back(f"/pdf/{document_id}")

    @app.get("/pdf/{document_id}", response_class=HTMLResponse)
    def pdf_detail(request: Request, document_id: int, mode: str = "chapters", max_pages: int = 8, error: str = "", message: str = ""):
        try:
            document = curator.pdf_document(document_id)
            chunks, chapters = curator.pdf_plan(document_id, mode, max_pages)
        except CuratorError as failure:
            return back("/pdf", error=str(failure))
        return render(
            request,
            "pdf.html",
            document=document,
            chunks=chunks,
            chapters=chapters,
            imported=curator.pdf_imported(document_id),
            mode=mode,
            max_pages=max_pages,
            error=error,
            message=message,
        )

    @app.post("/pdf/{document_id}/import")
    def pdf_import(
        document_id: int,
        keys: list[str] = Form(default_factory=list),
        mode: str = Form("chapters"),
        max_pages: int = Form(8),
    ):
        if not keys:
            return back(f"/pdf/{document_id}", error="Selecciona al menos una parte.")
        try:
            title = curator.pdf_document(document_id)["title"]
        except CuratorError as error:
            return back("/pdf", error=str(error))

        def work(task):
            return imported(len(curator.import_pdf_chunks(document_id, keys, mode, max_pages, task)), "parte", "partes", [], feminine=True)

        label = f"Importando {plural(len(keys), 'parte', 'partes')} de «{title}»"
        curator.start_job("import", label, work, f"/pdf/{document_id}")
        return back("/", message=f"{label}. El avance se ve en el bloque «Actividad».")

    @app.get("/audit", response_class=HTMLResponse)
    def audit_page(request: Request, status: str = "open", error: str = "", message: str = ""):
        if status not in ("open", "dismissed", "resolved"):
            status = "open"
        auditor = curator.auditor
        url = curator.confluence.page_url if curator.confluence else (lambda page_id: "")
        findings = []
        for row in auditor.findings(status):
            section = curator.section(row["page_id"], row["section_index"])
            findings.append(
                {
                    "row": row,
                    "url": url(row["page_id"]),
                    "other_url": url(row["other_page_id"]) if row["other_page_id"] else "",
                    "heading": section.heading if section else "",
                }
            )
        return render(
            request,
            "audit.html",
            audit=auditor.settings(),
            counts=auditor.counts(),
            findings=findings,
            status=status,
            KINDS=KINDS,
            error=error,
            message=message,
        )

    @app.post("/audit/settings")
    def audit_settings(
        enabled: str = Form(""), interval: int = Form(24), stale_days: int = Form(180), max_pairs: int = Form(30)
    ):
        try:
            curator.auditor.save(enabled == "1", interval, stale_days, max_pairs)
        except ValueError as error:
            return back("/audit", error=str(error))
        return back("/audit", message="Configuración guardada.")

    @app.post("/audit/run")
    def audit_run():
        if not curator.auditor.start():
            return back("/audit", error="Ya hay una revisión en curso.")
        return back("/audit", message="Revisión iniciada; la página se actualiza sola.")

    @app.post("/audit/findings/{finding_id}/fix")
    def audit_fix(finding_id: int):
        try:
            note_id = curator.auditor.fix(finding_id)
        except (ValueError, CuratorError) as error:
            return back("/audit", error=str(error))
        return back(f"/notes/{note_id}")

    @app.post("/audit/findings/{finding_id}/dismiss")
    def audit_dismiss(finding_id: int):
        curator.auditor.dismiss(finding_id)
        return back("/audit", message="Hallazgo descartado.")

    @app.get("/index", response_class=HTMLResponse)
    def index_page(request: Request, error: str = "", message: str = ""):
        return render(
            request,
            "index.html",
            tree=curator.page_tree(),
            pages=curator.pages(),
            last_sync=curator.db.get_meta("last_sync"),
            error=error,
            message=message,
        )

    @app.post("/sync")
    def sync():
        if curator.confluence is None:
            return back("/index", error="Confluence no está configurado.")
        curator.start_job("sync", "Sincronizando el índice", lambda task: f"Índice actualizado: {curator.sync()} páginas.", "/index")
        return back("/index", message="Sincronizando el índice. El avance se ve en el bloque «Actividad».")

    @app.get("/activity")
    def activity():
        return activity_state()

    @app.post("/activity/stop")
    def activity_stop(request: Request):
        message = curator.stop()
        if "application/json" in request.headers.get("accept", ""):
            return {"message": message}
        return back(request.headers.get("referer", "/").split("?")[0] or "/", message=message)

    @app.post("/activity/resume")
    def activity_resume(request: Request):
        curator.resume()
        if "application/json" in request.headers.get("accept", ""):
            return {"message": "Revisiones automáticas reanudadas."}
        return back(request.headers.get("referer", "/").split("?")[0] or "/", message="Revisiones automáticas reanudadas.")

    @app.get("/search", response_class=HTMLResponse)
    def search(request: Request, q: str = ""):
        return render(request, "search.html", q=q, results=curator.search(q, 20) if q.strip() else [])

    @app.get("/history", response_class=HTMLResponse)
    def history(request: Request):
        return render(request, "history.html", rows=curator.history())

    @app.get("/demo/pages/{page_id}", response_class=HTMLResponse)
    def demo_page(page_id: str):
        page = curator.page(page_id)
        if page is None or not page_id or curator.confluence is None or not hasattr(curator.confluence, "attachments"):
            return Response(status_code=404)
        header = f"<h1>{html.escape(page.title)}</h1><p><em>Versión {page.version} (demo)</em></p>"
        return HTMLResponse(preview(header + page.body, 0))

    return app
