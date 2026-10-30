"""Revisión periódica de la documentación: páginas desactualizadas o que se contradicen entre sí.

Hay tres tipos de hallazgo:
- ticket: la página menciona un ticket de Jira que cambió después de la última edición de la página.
- contradiction: dos secciones parecidas de páginas distintas afirman cosas incompatibles (requiere IA).
- stale: la página lleva más de N días sin editarse.

Un hallazgo abierto se da por resuelto cuando cambia alguna de sus páginas; si la siguiente revisión lo vuelve a
encontrar, se reabre. Los descartados no vuelven a aparecer mientras no cambie lo que los originó.
"""

import hashlib
import re
import threading
from datetime import datetime, timedelta, timezone

from . import storage
from .advisor import ContradictionCheck, SectionRef, advise
from .confluence import ConfluenceError
from .db import now
from .search import BM25, Document

ISSUE_KEY = re.compile(r"\b[A-Z][A-Z0-9_]+-\d+\b")
MIN_SECTION_TEXT = 40
KINDS = {"contradiction": "Contradicción", "ticket": "Ticket actualizado", "stale": "Sin cambios hace tiempo"}


def parse_time(value: str) -> datetime | None:
    if not value:
        return None
    for parse in (
        datetime.fromisoformat,
        lambda v: datetime.strptime(v, "%Y-%m-%dT%H:%M:%S.%f%z"),
        lambda v: datetime.fromisoformat(v.replace("Z", "+00:00")),
    ):
        try:
            moment = parse(value)
            return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


class Auditor:
    def __init__(self, curator):
        self.curator = curator
        self.db = curator.db
        self.lock = threading.Lock()

    # Configuración ----------------------------------------------------------

    def settings(self) -> dict:
        get = self.db.get_meta
        return {
            "enabled": get("audit_enabled") == "1",
            "interval": int(get("audit_interval", "24")),
            "stale_days": int(get("audit_stale_days", "180")),
            "max_pairs": int(get("audit_max_pairs", "30")),
            "last_run": get("audit_last_run"),
            "last_result": get("audit_last_result"),
            "last_error": get("audit_last_error"),
            "running": self.lock.locked(),
        }

    def save(self, enabled: bool, interval: int, stale_days: int, max_pairs: int) -> None:
        if interval < 1:
            raise ValueError("El intervalo mínimo es de 1 hora.")
        if stale_days < 0 or not 0 <= max_pairs <= 500:
            raise ValueError("Revisa los valores: los días no pueden ser negativos y las comparaciones van de 0 a 500.")
        self.db.set_meta("audit_enabled", "1" if enabled else "")
        self.db.set_meta("audit_interval", str(interval))
        self.db.set_meta("audit_stale_days", str(stale_days))
        self.db.set_meta("audit_max_pairs", str(max_pairs))

    def due(self, moment: datetime | None = None) -> bool:
        settings = self.settings()
        if not settings["enabled"] or settings["running"]:
            return False
        last = parse_time(settings["last_run"])
        return last is None or (moment or datetime.now(timezone.utc)) - last >= timedelta(hours=settings["interval"])

    def start(self) -> bool:
        """Lanza la revisión en segundo plano. Devuelve False si ya hay una en curso."""
        if self.lock.locked():
            return False
        threading.Thread(target=self._run_quietly, name="curator-audit", daemon=True).start()
        return True

    def _run_quietly(self) -> None:
        try:
            self.run()
        except Exception:
            pass  # queda en audit_last_error

    # Revisión ---------------------------------------------------------------

    def run(self) -> str:
        if not self.lock.acquire(blocking=False):
            return "Ya hay una revisión en curso."
        try:
            result = "Revisión detenida antes de terminar."
            with self.curator.activity.task("audit", "Revisión de la documentación", "/audit") as task:
                result = self._run(task)
                task.result = f"Revisión de la documentación: {result}"
            self.db.set_meta("audit_last_result", result)
            self.db.set_meta("audit_last_error", "")
            return result
        except Exception as error:
            self.db.set_meta("audit_last_error", str(error))
            raise
        finally:
            self.db.set_meta("audit_last_run", now())
            self.lock.release()

    def _run(self, task) -> str:
        settings = self.settings()
        if self.curator.confluence is not None:
            task.update(detail="Sincronizando el índice")
            self.curator.sync()
        task.check()
        pages = {p.id: p for p in self.curator.pages()}
        self._resolve_changed(pages)
        notes = []
        if self.curator.jira is not None:
            task.update(detail="Buscando tickets de Jira mencionados")
            self._tickets(pages, notes)
            task.check()
        if settings["stale_days"] > 0:
            self._stale(pages, settings["stale_days"])
        compared, cached = 0, 0
        if getattr(self.curator.advisor, "can_compare", False) and settings["max_pairs"] > 0:
            _, compared, cached = self._contradictions(pages, settings["max_pairs"], task)
        else:
            notes.append("las contradicciones necesitan IA")
        open_count = self.db.one("SELECT COUNT(*) AS n FROM audit_findings WHERE status = 'open'")["n"]
        parts = [f"{open_count} hallazgos abiertos", f"{len(pages)} páginas revisadas"]
        if compared or cached:
            parts.append(f"{compared} comparaciones nuevas" + (f", {cached} ya revisadas" if cached else ""))
        return "; ".join(parts + notes) + "."

    def _resolve_changed(self, pages: dict) -> None:
        """Un hallazgo abierto se resuelve cuando su página (o la otra página) cambió o ya no existe."""
        for row in self.db.all("SELECT * FROM audit_findings WHERE status = 'open'"):
            page = pages.get(row["page_id"])
            other = pages.get(row["other_page_id"]) if row["other_page_id"] else None
            changed = page is None or page.version != row["page_version"]
            if row["other_page_id"]:
                changed = changed or other is None or other.version != row["other_page_version"]
            if changed:
                self.db.execute(
                    "UPDATE audit_findings SET status = 'resolved', updated_at = ? WHERE id = ?", (now(), row["id"])
                )

    def _record(self, kind: str, fingerprint: str, page, section_index: int, title: str, detail: str, **extra) -> str:
        """Crea el hallazgo o reabre uno resuelto que se repite; los descartados se respetan."""
        other = extra.get("other")
        values = dict(
            page_id=page.id,
            section_index=section_index,
            page_version=page.version,
            other_page_id=other.id if other else "",
            other_section_index=extra.get("other_section_index", -1),
            other_page_version=other.version if other else 0,
            issue_key=extra.get("issue_key", ""),
            title=title,
            detail=detail,
            suggestion=extra.get("suggestion", ""),
            confidence=extra.get("confidence", 1.0),
        )
        existing = self.db.one("SELECT id, status FROM audit_findings WHERE fingerprint = ?", (fingerprint,))
        if existing is None:
            columns = ", ".join(values)
            self.db.execute(
                f"INSERT INTO audit_findings(kind, fingerprint, {columns}, status, created_at, updated_at) "
                f"VALUES (?, ?, {', '.join('?' for _ in values)}, 'open', ?, ?)",
                (kind, fingerprint, *values.values(), now(), now()),
            )
        elif existing["status"] == "resolved":
            assignments = ", ".join(f"{k} = ?" for k in values)
            self.db.execute(
                f"UPDATE audit_findings SET {assignments}, status = 'open', updated_at = ? WHERE id = ?",
                (*values.values(), now(), existing["id"]),
            )
        return fingerprint

    def _tickets(self, pages: dict, notes: list[str]) -> list[str]:
        mentions: dict[str, list[tuple]] = {}
        for page in pages.values():
            for section in storage.split_sections(page.body):
                for key in set(ISSUE_KEY.findall(section.text)):
                    mentions.setdefault(key, [])
                    if all(p.id != page.id for p, _ in mentions[key]):
                        mentions[key].append((page, section.index))
        issues = self._fetch_issues(sorted(mentions))
        if len(issues) < len(mentions):
            notes.append(f"{len(mentions) - len(issues)} claves mencionadas no son tickets accesibles")
        found = []
        for key, issue in issues.items():
            issue_time = parse_time(issue.updated)
            for page, section_index in mentions[key]:
                page_time = parse_time(page.updated)
                if issue_time is None or page_time is None or issue_time <= page_time:
                    continue
                found.append(
                    self._record(
                        "ticket",
                        f"ticket:{page.id}:{key}:{issue.updated}",
                        page,
                        section_index,
                        f"{key} cambió después de la última edición de «{page.title}»",
                        f"El ticket «{issue.summary}» ({issue.status}) se actualizó el {issue_time:%Y-%m-%d}; "
                        f"la página se editó por última vez el {page_time:%Y-%m-%d}.",
                        issue_key=key,
                        suggestion="Importa el ticket para ver si la página debe reflejar el cambio.",
                    )
                )
        return found

    def _fetch_issues(self, keys: list[str]) -> dict:
        """Busca los tickets por lotes. JQL falla si una clave no existe: entonces se parte el lote hasta aislarla."""
        jira = self.curator.jira
        found = {}

        def fetch(batch: list[str]) -> None:
            if not batch:
                return
            try:
                for issue in jira.search(", ".join(batch), len(batch)):
                    if issue.key in batch:
                        found[issue.key] = issue
            except ConfluenceError as error:
                if error.status in (401, 403):
                    raise
                if len(batch) > 1:
                    middle = len(batch) // 2
                    fetch(batch[:middle])
                    fetch(batch[middle:])

        for start in range(0, len(keys), 50):
            fetch(keys[start : start + 50])
        return found

    def _stale(self, pages: dict, days: int) -> list[str]:
        limit = datetime.now(timezone.utc) - timedelta(days=days)
        found = []
        for page in pages.values():
            updated = parse_time(page.updated)
            if updated is None or updated >= limit or not storage.to_text(page.body):
                continue
            age = (datetime.now(timezone.utc) - updated).days
            found.append(
                self._record(
                    "stale",
                    f"stale:{page.id}:{page.version}",
                    page,
                    -1,
                    f"«{page.title}» no cambia hace {age} días",
                    f"Última edición: {updated:%Y-%m-%d}. Revisa si sigue vigente; si lo está, márcala como revisada.",
                )
            )
        return found

    def _contradictions(self, pages: dict, budget: int, task=None) -> tuple[list[str], int, int]:
        sections = {}
        documents = []
        for page in pages.values():
            for section in storage.split_sections(page.body):
                if len(section.text) >= MIN_SECTION_TEXT:
                    key = f"{page.id}#{section.index}"
                    sections[key] = (page, section)
                    documents.append(Document(key, f"{section.heading} {section.text}"))
        index = BM25(documents)
        pairs: dict[tuple[str, str], float] = {}
        for key, (page, section) in sections.items():
            hits = [(d, score) for d, score in index.search(f"{section.heading} {section.text}", 8) if not d.key.startswith(f"{page.id}#")]
            for document, score in hits[:2]:
                pair = tuple(sorted((key, document.key)))
                pairs[pair] = max(pairs.get(pair, 0), score)

        found, compared, cached = [], 0, 0
        for (key_a, key_b), _ in sorted(pairs.items(), key=lambda item: item[1], reverse=True):
            (page_a, section_a), (page_b, section_b) = sections[key_a], sections[key_b]
            pair_hash = hashlib.sha1(f"{section_a.storage}\0{section_b.storage}".encode()).hexdigest()
            row = self.db.one("SELECT result FROM audit_pairs WHERE pair_hash = ?", (pair_hash,))
            if row:
                result = ContradictionCheck.model_validate_json(row["result"])
                cached += 1
            elif compared < budget:
                if task:
                    task.check()
                    task.update(detail="Comparando secciones parecidas", done=compared, total=budget)
                result = advise(
                    self.curator.advisor.compare,
                    SectionRef(page_a.title, section_a.heading, section_a.storage, page_a.updated),
                    SectionRef(page_b.title, section_b.heading, section_b.storage, page_b.updated),
                    task=task,
                )
                compared += 1
                self.db.execute(
                    "INSERT OR REPLACE INTO audit_pairs(pair_hash, checked_at, result) VALUES (?, ?, ?)",
                    (pair_hash, now(), result.model_dump_json()),
                )
            else:
                continue
            if not result.contradicts or result.confidence < 0.5:
                continue
            # La sección que parece desactualizada es la que se corrige.
            (page, section), (other, other_section) = (
                ((page_b, section_b), (page_a, section_a)) if result.outdated == "b" else ((page_a, section_a), (page_b, section_b))
            )
            found.append(
                self._record(
                    "contradiction",
                    f"contradiction:{pair_hash}",
                    page,
                    section.index,
                    f"«{page.title} › {section.heading}» contradice a «{other.title} › {other_section.heading}»",
                    result.explanation,
                    other=other,
                    other_section_index=other_section.index,
                    suggestion=result.suggestion,
                    confidence=result.confidence,
                )
            )
        return found, compared, cached

    # Acciones ---------------------------------------------------------------

    def findings(self, status: str = "open"):
        return self.db.all(
            "SELECT f.*, p.title AS page_title, o.title AS other_title FROM audit_findings f "
            "LEFT JOIN pages p ON p.id = f.page_id LEFT JOIN pages o ON o.id = f.other_page_id "
            "WHERE f.status = ? ORDER BY CASE f.kind WHEN 'contradiction' THEN 0 WHEN 'ticket' THEN 1 ELSE 2 END, "
            "f.confidence DESC, f.id",
            (status,),
        )

    def counts(self) -> dict:
        rows = self.db.all("SELECT status, COUNT(*) AS n FROM audit_findings GROUP BY status")
        return {row["status"]: row["n"] for row in rows}

    def finding(self, finding_id: int):
        row = self.db.one("SELECT * FROM audit_findings WHERE id = ?", (finding_id,))
        if row is None:
            raise ValueError("El hallazgo no existe.")
        return row

    def dismiss(self, finding_id: int) -> None:
        self.db.execute(
            "UPDATE audit_findings SET status = 'dismissed', updated_at = ? WHERE id = ?", (now(), finding_id)
        )

    def fix(self, finding_id: int) -> int:
        """Crea una nota dirigida a la sección afectada; sigue el flujo normal de propuesta y aprobación."""
        finding = self.finding(finding_id)
        if finding["kind"] == "ticket":
            note_ids, errors = self.curator.import_issues([finding["issue_key"]])
            if not note_ids:
                raise ValueError(errors[0] if errors else "No se pudo importar el ticket.")
            note_id = note_ids[0]
        else:
            lines = [f"Revisión de la documentación: {finding['title']}.", finding["detail"]]
            if finding["suggestion"]:
                lines.append(f"Sugerencia: {finding['suggestion']}")
            if finding["other_page_id"]:
                other = self.curator.section(finding["other_page_id"], finding["other_section_index"])
                other_page = self.curator.page(finding["other_page_id"])
                if other and other_page:
                    lines += ["", f"Lo que dice «{other_page.title} › {other.heading}»:", other.text]
            note_id = self.curator.create_note("\n".join(lines), "Revisión", f"Hallazgo #{finding_id}", [])
            self.curator.enqueue(
                note_id,
                hint="Corrige la sección señalada por la revisión; no inventes datos que la nota no respalde.",
                target_page=finding["page_id"],
                target_section=finding["section_index"],
            )
        self.db.execute("UPDATE audit_findings SET note_id = ?, updated_at = ? WHERE id = ?", (note_id, now(), finding_id))
        return note_id
