import json
import mimetypes
from dataclasses import dataclass

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
    def __init__(self, db: Database, advisor: Advisor, confluence=None, space_key: str = "", candidates: int = 10):
        self.db = db
        self.advisor = advisor
        self.confluence = confluence
        self.space_key = space_key
        self.candidate_limit = candidates

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

    # Consultas para la interfaz --------------------------------------------

    def recent_notes(self, limit: int = 30):
        return self.db.all(
            "SELECT n.*, "
            "(SELECT COUNT(*) FROM proposals p WHERE p.note_id = n.id AND p.status = 'pending') AS pending, "
            "(SELECT COUNT(*) FROM proposals p WHERE p.note_id = n.id AND p.status = 'applied') AS applied "
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
