import json
import mimetypes
import queue
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from . import storage
from .advisor import Advisor, AdvisorInput, Analysis, Candidate
from .confluence import ConfluenceError
from .db import Database, loads, now
from .search import BM25, Document


class CuratorError(Exception):
    pass


@dataclass
class IndexedPage:
    id: str
    title: str
    parent_id: str | None
    version: int
    body: str


class Curator:
    def __init__(
        self,
        db: Database,
        advisor: Advisor,
        confluence=None,
        space_key: str = "",
        candidates: int = 10,
        jira=None,
        jira_max_images: int = 5,
        background: bool = True,
        pdf_max_scanned: int = 5,
    ):
        self.db = db
        self.advisor = advisor
        self.confluence = confluence
        self.space_key = space_key
        self.candidate_limit = candidates
        self.jira = jira
        self.jira_max_images = jira_max_images
        self.pdf_max_scanned = pdf_max_scanned
        self.background = background
        self.pending: queue.Queue = queue.Queue()
        self.worker: threading.Thread | None = None
        self.worker_lock = threading.Lock()
        self.watch_lock = threading.Lock()
        self.watcher: threading.Thread | None = None

    # Índice ---------------------------------------------------------------

    def _require_confluence(self):
        if self.confluence is None:
            raise CuratorError("Confluence no está configurado (revisa las variables CONFLUENCE_* en .env).")
        return self.confluence

    def space_id(self) -> str:
        cached = self.db.get_meta("space_id")
        if cached and self.db.get_meta("space_key") == self.space_key:
            return cached
        space_id = self._require_confluence().space_id(self.space_key)
        self.db.set_meta("space_id", space_id)
        self.db.set_meta("space_key", self.space_key)
        return space_id

    def sync(self) -> int:
        pages = self._require_confluence().list_pages(self.space_id())
        self.db.replace_pages(pages)
        self.db.set_meta("last_sync", now())
        return len(pages)

    def pages(self) -> list[IndexedPage]:
        return [IndexedPage(**dict(row)) for row in self.db.all("SELECT * FROM pages ORDER BY title")]

    def page(self, page_id: str) -> IndexedPage | None:
        row = self.db.one("SELECT * FROM pages WHERE id = ?", (page_id,))
        return IndexedPage(**dict(row)) if row else None

    def page_tree(self) -> str:
        pages = self.pages()
        ids = {p.id for p in pages}
        children: dict[str | None, list[IndexedPage]] = {}
        for p in pages:
            children.setdefault(p.parent_id if p.parent_id in ids else None, []).append(p)
        lines: list[str] = []

        def walk(parent: str | None, depth: int) -> None:
            for p in children.get(parent, []):
                lines.append(f"{'  ' * depth}- [{p.id}] {p.title}")
                walk(p.id, depth + 1)

        walk(None, 0)
        return "\n".join(lines)

    def search(self, query: str, limit: int | None = None) -> list[Candidate]:
        documents, sections = [], {}
        for p in self.pages():
            for s in storage.split_sections(p.body):
                key = f"{p.id}#{s.index}"
                documents.append(Document(key, f"{p.title} {p.title} {s.heading} {s.heading} {s.text}"))
                sections[key] = (p, s)
        results = BM25(documents).search(query, limit or self.candidate_limit)
        return [
            Candidate(p.id, p.title, s.index, s.heading, s.storage, score)
            for document, score in results
            for p, s in [sections[document.key]]
        ]

    def section(self, page_id: str, index: int) -> storage.Section | None:
        p = self.page(page_id)
        if p is None:
            return None
        sections = storage.split_sections(p.body)
        return sections[index] if 0 <= index < len(sections) else None

    # Notas y propuestas ---------------------------------------------------

    def create_note(self, text: str, source_type: str, source_ref: str, images: list[tuple[str, str, bytes]]) -> int:
        if not text.strip() and not images:
            raise CuratorError("Escribe un texto o agrega al menos una imagen.")
        cursor = self.db.execute(
            "INSERT INTO notes(created_at, text, source_type, source_ref) VALUES (?, ?, ?, ?)",
            (now(), text.strip(), source_type, source_ref.strip()),
        )
        note_id = cursor.lastrowid
        for index, (filename, mime, data) in enumerate(images, start=1):
            self.db.execute(
                "INSERT INTO note_images(note_id, idx, filename, mime, data) VALUES (?, ?, ?, ?, ?)",
                (note_id, index, filename, mime, data),
            )
        return note_id

    def note(self, note_id: int):
        note = self.db.one("SELECT * FROM notes WHERE id = ?", (note_id,))
        if note is None:
            raise CuratorError("La nota no existe.")
        return note

    def note_images(self, note_id: int):
        return self.db.all("SELECT * FROM note_images WHERE note_id = ? ORDER BY idx", (note_id,))

    def analyze(self, note_id: int, hint: str = "", target_page: str = "", target_section: int = -1) -> None:
        note = self.note(note_id)
        source = " ".join(x for x in [note["source_type"], note["source_ref"]] if x)
        candidates = self.search(f"{note['text']} {hint}")
        if target_page:
            page = self.page(target_page)
            if page is None:
                raise CuratorError("La página indicada no está en el índice.")
            target = self.section(target_page, target_section) if target_section >= 0 else None
            location = f"la página «{page.title}» (page_id {page.id})"
            if target:
                location = f"la sección «{target.heading}» (section_index {target.index}) de {location}"
                candidates = [c for c in candidates if (c.page_id, c.section_index) != (page.id, target.index)]
                candidates.insert(0, Candidate(page.id, page.title, target.index, target.heading, target.storage, 0))
            hint = f"La información va en {location}. {hint}".strip()
        data = AdvisorInput(
            note_text=note["text"],
            source=source,
            images=[(row["mime"], row["data"]) for row in self.note_images(note_id)],
            candidates=candidates,
            page_tree=self.page_tree(),
            hint=hint,
        )
        self.db.execute("UPDATE proposals SET status = 'superseded' WHERE note_id = ? AND status = 'pending'", (note_id,))
        try:
            analysis = self.advisor.analyze(data)
        except Exception as error:  # el error se muestra en la página de la nota
            self.db.execute("UPDATE notes SET error = ? WHERE id = ?", (f"No se pudo analizar: {error}", note_id))
            return
        self._store(note_id, analysis)

    def enqueue(self, note_id: int, **options) -> None:
        """Analiza la nota en segundo plano, de a una por vez, para no bloquear la interfaz."""
        self.db.execute("UPDATE notes SET analyzing = 1, error = '' WHERE id = ?", (note_id,))
        if not self.background:
            self._run(note_id, options)
            return
        self.pending.put((note_id, options))
        with self.worker_lock:
            if self.worker is None or not self.worker.is_alive():
                self.worker = threading.Thread(target=self._work, name="curator-analysis", daemon=True)
                self.worker.start()

    def resume_pending(self) -> None:
        """Retoma los análisis que quedaron a medias si la aplicación se reinició."""
        for row in self.db.all("SELECT id FROM notes WHERE analyzing = 1 ORDER BY id"):
            self.enqueue(row["id"])

    def wait_idle(self) -> None:
        self.pending.join()

    def _work(self) -> None:
        while True:
            note_id, options = self.pending.get()
            try:
                self._run(note_id, options)
            finally:
                self.pending.task_done()

    def _run(self, note_id: int, options: dict) -> None:
        try:
            self.analyze(note_id, **options)
        except Exception as error:
            self.db.execute("UPDATE notes SET error = ? WHERE id = ?", (f"No se pudo analizar: {error}", note_id))
        finally:
            self.db.execute("UPDATE notes SET analyzing = 0 WHERE id = ?", (note_id,))

    def _store(self, note_id: int, analysis: Analysis) -> None:
        self.db.execute(
            "UPDATE notes SET summary = ?, no_change_reason = ?, alternatives = ?, error = '' WHERE id = ?",
            (
                analysis.note_summary,
                analysis.no_change_reason,
                json.dumps([a.model_dump() for a in analysis.alternatives], ensure_ascii=False),
                note_id,
            ),
        )
        for change in analysis.changes:
            old_storage, error = "", ""
            page_id = change.page_id
            if change.action == "update_section":
                section = self.section(page_id, change.section_index)
                if section is None:
                    error = "La sección propuesta no existe en el índice; elige otra ubicación."
                else:
                    old_storage = section.storage
            elif change.action == "append_to_page" and self.page(page_id) is None:
                error = "La página propuesta no existe en el índice; elige otra ubicación."
            elif change.action == "create_page" and page_id and self.page(page_id) is None:
                page_id = ""
            if not error:
                try:
                    storage.validate_fragment(change.content_storage)
                except ValueError as invalid:
                    error = str(invalid)
            self.db.execute(
                "INSERT INTO proposals(note_id, action, page_id, section_index, old_storage, new_storage, new_title, "
                "summary, rationale, confidence, conflicts, status, error, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)",
                (
                    note_id,
                    change.action,
                    page_id,
                    change.section_index,
                    old_storage,
                    change.content_storage,
                    change.new_page_title,
                    change.summary,
                    change.rationale,
                    max(0.0, min(1.0, change.confidence)),
                    json.dumps(change.conflicts, ensure_ascii=False),
                    error,
                    now(),
                ),
            )

    def proposals(self, note_id: int, include_old: bool = False):
        where = "" if include_old else "AND status != 'superseded'"
        return self.db.all(f"SELECT * FROM proposals WHERE note_id = ? {where} ORDER BY id", (note_id,))

    def proposal(self, proposal_id: int):
        row = self.db.one("SELECT * FROM proposals WHERE id = ?", (proposal_id,))
        if row is None:
            raise CuratorError("La propuesta no existe.")
        return row

    def reject(self, proposal_id: int) -> None:
        self.db.execute(
            "UPDATE proposals SET status = 'rejected', decided_at = ? WHERE id = ? AND status = 'pending'",
            (now(), proposal_id),
        )

    def apply(self, proposal_id: int, content: str, new_title: str = "") -> str:
        proposal = self.proposal(proposal_id)
        if proposal["status"] != "pending":
            raise CuratorError("Esta propuesta ya no está pendiente.")
        confluence = self._require_confluence()
        storage.validate_fragment(content)
        note_id = proposal["note_id"]
        images = {row["idx"]: row for row in self.note_images(note_id)}
        used = sorted({n for n in images if f"[[IMAGEN:{n}]]" in content})
        missing = [m for m in storage.IMAGE_PLACEHOLDER.findall(content) if int(m) not in images]
        if missing:
            raise CuratorError(f"La nota no tiene la imagen {missing[0]}.")

        def filename(n: int) -> str:
            original = images[n]["filename"]
            extension = original.rsplit(".", 1)[-1] if "." in original else (mimetypes.guess_extension(images[n]["mime"]) or ".png").lstrip(".")
            return f"nota-{note_id}-imagen-{n}.{extension}"

        final = storage.replace_placeholders(content, lambda n: storage.image_markup(filename(n)))
        message = f"{proposal['summary']} (curator, nota {note_id})"
        try:
            if proposal["action"] == "create_page":
                title = (new_title or proposal["new_title"]).strip()
                if not title:
                    raise CuratorError("La página nueva necesita un título.")
                page = confluence.create_page(self.space_id(), proposal["page_id"] or None, title, final)
                self._upload(page.id, used, images, filename, message)
            else:
                page = confluence.get_page(proposal["page_id"])
                if proposal["action"] == "update_section":
                    section = storage.find_section(page.body, proposal["old_storage"], proposal["section_index"])
                    if section is None:
                        raise CuratorError(
                            "La sección cambió en Confluence desde el análisis. Sincroniza el índice y vuelve a analizar la nota."
                        )
                    body = storage.replace_section(page.body, section, final)
                else:
                    body = page.body + final
                self._upload(page.id, used, images, filename, message)
                page = confluence.update_page(page, body, message)
        except ConfluenceError as error:
            self.db.execute("UPDATE proposals SET error = ? WHERE id = ?", (str(error), proposal_id))
            raise CuratorError(str(error)) from error
        self.db.upsert_page(page)
        url = confluence.page_url(page.id)
        self.db.execute(
            "UPDATE proposals SET status = 'applied', new_storage = ?, new_title = ?, page_id = ?, result_url = ?, "
            "error = '', decided_at = ? WHERE id = ?",
            (content, new_title or proposal["new_title"], page.id, url, now(), proposal_id),
        )
        return url

    def _upload(self, page_id: str, used: list[int], images: dict, filename, message: str) -> None:
        for n in used:
            self.confluence.upload_attachment(page_id, filename(n), images[n]["data"], images[n]["mime"], message)

    # Jira -------------------------------------------------------------------

    def _require_jira(self):
        if self.jira is None:
            raise CuratorError("Jira no está configurado (revisa las variables JIRA_* o CONFLUENCE_* en .env).")
        return self.jira

    def jira_search(self, query: str, limit: int = 50) -> list[dict]:
        if not query.strip():
            raise CuratorError("Escribe una consulta JQL o claves de tickets.")
        try:
            issues = self._require_jira().search(query, limit)
        except ConfluenceError as error:
            raise CuratorError(str(error)) from error
        self.db.set_meta("last_jql", query.strip())
        return self._annotate(issues)

    def _annotate(self, issues) -> list[dict]:
        """Marca cada ticket con su última importación y si cambió en Jira desde entonces."""
        results = []
        for issue in issues:
            last = self.db.one(
                "SELECT * FROM jira_imports WHERE issue_key = ? ORDER BY imported_at DESC, rowid DESC LIMIT 1", (issue.key,)
            )
            changed = bool(last) and _jira_time(issue.updated) > _jira_time(last["issue_updated"])
            results.append({"issue": issue, "last": last, "changed": changed})
        return results

    def import_issues(self, keys: list[str]) -> tuple[list[int], list[str]]:
        """Crea una nota por ticket (descripción, comentarios e imágenes) y la manda a analizar."""
        jira = self._require_jira()
        note_ids, errors = [], []
        for key in dict.fromkeys(keys):
            try:
                issue = jira.issue(key, self.jira_max_images, 10 * 1024 * 1024)
            except ConfluenceError as error:
                errors.append(f"{key}: {error}")
                continue
            note_id = self.create_note(f"{issue.text}\n\nEnlace: {issue.url}", "Jira", issue.key, issue.images)
            self.db.execute(
                "INSERT INTO jira_imports(issue_key, issue_updated, note_id, imported_at) VALUES (?, ?, ?, ?)",
                (issue.key, issue.updated, note_id, now()),
            )
            note_ids.append(note_id)
        for note_id in note_ids:
            self.enqueue(note_id)
        return note_ids, errors

    # Revisión automática de Jira -------------------------------------------

    WATCH_BATCH = 20

    def watch_settings(self) -> dict:
        get = self.db.get_meta
        return {
            "jql": get("watch_jql"),
            "interval": int(get("watch_interval", "60")),
            "enabled": get("watch_enabled") == "1",
            "since": get("watch_since"),
            "last_run": get("watch_last_run"),
            "last_result": get("watch_last_result"),
            "last_error": get("watch_last_error"),
        }

    def save_watch(self, jql: str, interval: int, enabled: bool) -> None:
        jql = jql.strip()
        if enabled and not jql:
            raise CuratorError("Escribe la consulta JQL que se revisará.")
        if interval < 5:
            raise CuratorError("El intervalo mínimo es de 5 minutos.")
        current = self.watch_settings()
        if enabled:
            try:
                self._require_jira().search(watch_query(jql, now()), 1)
            except ConfluenceError as error:
                raise CuratorError(f"Jira rechazó la consulta: {error}") from error
            if not current["enabled"] or jql != current["jql"] or not current["since"]:
                # Solo se traen los tickets que cambien desde ahora; los anteriores se importan a mano.
                self.db.set_meta("watch_since", now())
        self.db.set_meta("watch_jql", jql)
        self.db.set_meta("watch_interval", str(interval))
        self.db.set_meta("watch_enabled", "1" if enabled else "")

    def run_watch(self) -> str:
        """Importa los tickets de la consulta guardada que son nuevos o cambiaron desde su última importación."""
        if not self.watch_lock.acquire(blocking=False):
            return "Ya hay una revisión en curso."
        try:
            settings = self.watch_settings()
            if not settings["jql"]:
                raise CuratorError("No hay una consulta guardada para revisar.")
            since = datetime.fromisoformat(settings["since"] or now())
            try:
                issues = self._require_jira().search(watch_query(settings["jql"], since.isoformat()), 100)
            except ConfluenceError as error:
                raise CuratorError(str(error)) from error
            due = [
                r["issue"].key
                for r in self._annotate(issues)
                if r["changed"] or (not r["last"] and _jira_time(r["issue"].updated) >= since)
            ]
            note_ids, errors = self.import_issues(due[: self.WATCH_BATCH]) if due else ([], [])
            pending = len(due) - self.WATCH_BATCH
            parts = [f"{len(note_ids)} tickets importados" if note_ids else "sin tickets nuevos ni cambios"]
            if pending > 0:
                parts.append(f"{pending} quedan para la próxima revisión")
            result = "; ".join(parts) + "."
            self.db.set_meta("watch_last_result", result)
            self.db.set_meta("watch_last_error", " · ".join(errors))
            return result
        except Exception as error:
            self.db.set_meta("watch_last_error", str(error))
            raise
        finally:
            self.db.set_meta("watch_last_run", now())
            self.watch_lock.release()

    def watch_due(self, moment: datetime | None = None) -> bool:
        settings = self.watch_settings()
        if not settings["enabled"] or self.jira is None:
            return False
        if not settings["last_run"]:
            return True
        elapsed = (moment or datetime.now(timezone.utc)) - datetime.fromisoformat(settings["last_run"])
        return elapsed >= timedelta(minutes=settings["interval"])

    def start_watcher(self, poll_seconds: int = 30) -> None:
        def loop() -> None:
            while True:
                if self.watch_due():
                    try:
                        self.run_watch()
                    except Exception:
                        pass  # el error queda guardado en watch_last_error y se muestra en la interfaz
                time.sleep(poll_seconds)

        if self.watcher is None:
            self.watcher = threading.Thread(target=loop, name="curator-jira-watch", daemon=True)
            self.watcher.start()

    # PDFs -------------------------------------------------------------------

    def add_pdf(self, filename: str, data: bytes, source_type: str) -> int:
        from . import pdf

        try:
            reader = pdf.open_pdf(data)
        except pdf.PdfError as error:
            raise CuratorError(str(error)) from error
        cursor = self.db.execute(
            "INSERT INTO pdf_documents(filename, title, source_type, pages, data, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (filename, pdf.document_title(reader, filename), source_type, len(reader.pages), data, now()),
        )
        return cursor.lastrowid

    def pdf_documents(self):
        return self.db.all(
            "SELECT d.id, d.filename, d.title, d.pages, d.source_type, d.created_at, "
            "(SELECT COUNT(DISTINCT chunk_key) FROM pdf_imports i WHERE i.document_id = d.id) AS imported "
            "FROM pdf_documents d ORDER BY d.id DESC"
        )

    def pdf_document(self, document_id: int):
        row = self.db.one("SELECT * FROM pdf_documents WHERE id = ?", (document_id,))
        if row is None:
            raise CuratorError("El PDF no existe.")
        return row

    def pdf_plan(self, document_id: int, mode: str = "chapters", max_pages: int = 8):
        """Partes del PDF (se guardan para no releerlo en cada visita) y si salen de los capítulos."""
        from . import pdf

        if not 1 <= max_pages <= 50:
            raise CuratorError("Las páginas por parte deben estar entre 1 y 50.")
        document = self.pdf_document(document_id)
        params = json.dumps([mode, max_pages, self.pdf_max_scanned])
        if document["plan_params"] == params:
            cached = json.loads(document["plan"])
            return [pdf.Chunk(**c) for c in cached["chunks"]], cached["chapters"]
        try:
            chunks, chapters = pdf.plan(document["data"], mode, max_pages, self.pdf_max_scanned)
        except pdf.PdfError as error:
            raise CuratorError(str(error)) from error
        plan = json.dumps({"chunks": [c.to_dict() for c in chunks], "chapters": chapters}, ensure_ascii=False)
        self.db.execute("UPDATE pdf_documents SET plan_params = ?, plan = ? WHERE id = ?", (params, plan, document_id))
        return chunks, chapters

    def pdf_imported(self, document_id: int) -> dict[str, int]:
        rows = self.db.all(
            "SELECT chunk_key, note_id FROM pdf_imports WHERE document_id = ? ORDER BY imported_at, rowid", (document_id,)
        )
        return {row["chunk_key"]: row["note_id"] for row in rows}

    def import_pdf_chunks(self, document_id: int, keys: list[str], mode: str, max_pages: int) -> list[int]:
        """Crea una nota por parte elegida; las páginas sin texto viajan como imágenes."""
        from . import pdf

        document = self.pdf_document(document_id)
        chunks, _ = self.pdf_plan(document_id, mode, max_pages)
        note_ids = []
        for chunk in chunks:
            if chunk.key not in keys:
                continue
            lines = [f"{document['title']}: {chunk.title} (páginas {chunk.pages})"]
            if len(chunk.scanned) == 1:
                lines.append(f"La página {chunk.scanned[0]} no tiene texto extraíble y se adjunta como imagen.")
            elif chunk.scanned:
                listed = ", ".join(str(p) for p in chunk.scanned)
                lines.append(f"Las páginas {listed} no tienen texto extraíble y se adjuntan como imágenes, en ese orden.")
            if chunk.text:
                lines += ["", chunk.text]
            images = pdf.render_pages(document["data"], chunk.scanned) if chunk.scanned else []
            note_id = self.create_note(
                "\n".join(lines), document["source_type"], f"{document['title']}, p. {chunk.pages}", images
            )
            self.db.execute(
                "INSERT INTO pdf_imports(document_id, chunk_key, note_id, imported_at) VALUES (?, ?, ?, ?)",
                (document_id, chunk.key, note_id, now()),
            )
            note_ids.append(note_id)
        for note_id in note_ids:
            self.enqueue(note_id)
        return note_ids

    # Consultas para la interfaz --------------------------------------------

    def recent_notes(self, limit: int = 30):
        return self.db.all(
            "SELECT n.*, "
            "(SELECT COUNT(*) FROM proposals p WHERE p.note_id = n.id AND p.status = 'pending') AS pending, "
            "(SELECT COUNT(*) FROM proposals p WHERE p.note_id = n.id AND p.status = 'applied') AS applied, "
            "(SELECT COUNT(*) FROM proposals p WHERE p.note_id = n.id) AS total "
            "FROM notes n ORDER BY n.id DESC LIMIT ?",
            (limit,),
        )

    def history(self, limit: int = 100):
        return self.db.all(
            "SELECT p.*, pg.title AS page_title FROM proposals p LEFT JOIN pages pg ON pg.id = p.page_id "
            "WHERE p.status IN ('applied', 'rejected') ORDER BY p.decided_at DESC LIMIT ?",
            (limit,),
        )

    def alternatives(self, note) -> list[dict]:
        result = []
        for alt in loads(note["alternatives"]):
            page = self.page(alt["page_id"])
            if page is None:
                continue
            section = self.section(page.id, alt["section_index"]) if alt["section_index"] >= 0 else None
            result.append({**alt, "page_title": page.title, "heading": section.heading if section else ""})
        return result


def watch_query(jql: str, since: str) -> str:
    """Restringe la consulta guardada a lo actualizado desde el día anterior a "since", más reciente primero.

    El filtro de JQL es por fecha (Jira la interpreta en la zona horaria de la cuenta); el corte exacto se hace después.
    """
    from .jira import to_jql

    base = re.sub(r"\s+ORDER\s+BY\s+.*$", "", to_jql(jql), flags=re.IGNORECASE | re.DOTALL).strip()
    day = (datetime.fromisoformat(since) - timedelta(days=1)).strftime("%Y-%m-%d")
    return f'({base}) AND updated >= "{day}" ORDER BY updated DESC'


def _jira_time(value: str) -> datetime:
    """Jira usa fechas como 2026-10-20T10:00:00.000-0500; se comparan como instantes, no como texto."""
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%f%z")
    except ValueError:
        return datetime.fromisoformat(value)
