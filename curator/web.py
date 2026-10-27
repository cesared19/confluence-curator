import base64
import difflib
import html
import secrets
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import storage
from .advisor import AnthropicAdvisor, MockAdvisor
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
IMAGE_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}


def build_curator(settings: Settings) -> Curator:
    import os

    if os.environ.get("CURATOR_DEMO") == "1":
        from .demo import FakeConfluence

        confluence, space_key = FakeConfluence(), "DEMO"
    elif settings.confluence_configured:
        from .confluence import ConfluenceClient

        confluence = ConfluenceClient(settings.confluence_base_url, settings.confluence_email, settings.confluence_api_token)
        space_key = settings.confluence_space_key
    else:
        confluence, space_key = None, ""
    advisor = (
        AnthropicAdvisor(settings.anthropic_model, settings.anthropic_effort)
        if settings.advisor == "anthropic"
        else MockAdvisor()
    )
    return Curator(Database(settings.database_path), advisor, confluence, space_key, settings.candidates)


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
    templates.env.globals.update(ACTIONS=ACTIONS, curator=curator, settings=settings, loads=loads)

    if settings.app_user and settings.app_password:

        @app.middleware("http")
        async def basic_auth(request: Request, call_next):
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

    @app.get("/", response_class=HTMLResponse)
    def inbox(request: Request, error: str = "", message: str = ""):
        return render(
            request,
            "inbox.html",
            notes=curator.recent_notes(),
            sources=SOURCES,
            pages=len(curator.pages()),
            last_sync=curator.db.get_meta("last_sync"),
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
        curator.analyze(note_id)
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
        try:
            curator.analyze(note_id, hint, page_id, int(section) if section else -1)
        except CuratorError as error:
            return back(f"/notes/{note_id}", error=str(error))
        return back(f"/notes/{note_id}", message="Nota analizada de nuevo.")

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
        try:
            count = curator.sync()
        except Exception as error:
            return back("/index", error=f"No se pudo sincronizar: {error}")
        return back("/index", message=f"Índice actualizado: {count} páginas.")

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
