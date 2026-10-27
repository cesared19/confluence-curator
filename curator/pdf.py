"""Lectura de PDFs (manuales del producto) y división en partes que se analizan como notas."""

import io
import re
from collections import Counter
from dataclasses import asdict, dataclass

from pypdf import PdfReader
from pypdf.errors import PdfReadError

MIN_TEXT = 30  # menos caracteres que esto en una página: se trata como escaneada


class PdfError(Exception):
    pass


@dataclass
class Chunk:
    title: str
    start: int  # páginas numeradas desde 1, inclusivas
    end: int
    text: str
    scanned: list[int]

    @property
    def key(self) -> str:
        return f"{self.start}-{self.end}"

    @property
    def pages(self) -> str:
        return f"{self.start}" if self.start == self.end else f"{self.start}–{self.end}"

    def to_dict(self) -> dict:
        return asdict(self)


def open_pdf(data: bytes) -> PdfReader:
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted and not reader.decrypt(""):
            raise PdfError("El PDF está protegido con contraseña.")
        len(reader.pages)
        return reader
    except (PdfReadError, ValueError, KeyError) as error:
        raise PdfError(f"No se pudo leer el PDF: {error}") from error


def document_title(reader: PdfReader, filename: str) -> str:
    title = ""
    try:
        title = (reader.metadata.title or "") if reader.metadata else ""
    except Exception:
        pass
    return title.strip() or re.sub(r"\.pdf$", "", filename, flags=re.IGNORECASE)


def outline(reader: PdfReader) -> list[tuple[str, int]]:
    """Capítulos según los marcadores del PDF: (título, página desde 0). Usa el primer nivel con más de una entrada."""

    def entries(items) -> list[tuple[str, int, list]]:
        result = []
        for index, item in enumerate(items):
            if isinstance(item, list):
                continue
            try:
                page = reader.get_destination_page_number(item)
            except Exception:
                continue
            children = items[index + 1] if index + 1 < len(items) and isinstance(items[index + 1], list) else []
            result.append((str(item.title).strip(), page, children))
        return result

    try:
        level = entries(reader.outline)
    except Exception:
        return []
    while len(level) == 1 and level[0][2]:
        level = entries(level[0][2])
    seen, chapters = set(), []
    for title, page, _ in sorted(level, key=lambda e: e[1]):
        if page not in seen and page is not None and page >= 0:
            seen.add(page)
            chapters.append((title or f"Página {page + 1}", page))
    return chapters if len(chapters) > 1 else []


def page_texts(reader: PdfReader) -> list[str]:
    """Texto de cada página, sin encabezados ni pies repetidos y con las palabras cortadas por guion unidas."""
    raw = []
    for page in reader.pages:
        try:
            raw.append(page.extract_text() or "")
        except Exception:
            raw.append("")
    lines = [[line.strip() for line in text.splitlines() if line.strip()] for text in raw]

    def normalize(line: str) -> str:
        return re.sub(r"\d+", "#", line.lower())

    repeated: set[str] = set()
    if len(lines) >= 4:
        counts = Counter()
        for page_lines in lines:
            counts.update({normalize(line) for line in page_lines[:2] + page_lines[-2:]})
        repeated = {line for line, count in counts.items() if count >= len(lines) / 2}

    texts = []
    for page_lines in lines:
        edges = set(range(min(2, len(page_lines)))) | set(range(max(0, len(page_lines) - 2), len(page_lines)))
        kept = [line for i, line in enumerate(page_lines) if not (i in edges and normalize(line) in repeated)]
        text = "\n".join(kept)
        text = re.sub(r"(\w)-\n(\w)", r"\1\2", text)
        texts.append(text.strip())
    return texts


def plan(data: bytes, mode: str, max_pages: int, max_scanned: int) -> tuple[list[Chunk], bool]:
    """Divide el PDF en partes. mode "chapters" usa los marcadores si existen; si no, bloques de max_pages.

    Devuelve las partes y si se usaron los capítulos. Las partes con páginas escaneadas se limitan a max_scanned
    páginas, porque esas páginas viajan como imágenes.
    """
    reader = open_pdf(data)
    texts = page_texts(reader)
    total = len(texts)
    if total == 0:
        raise PdfError("El PDF no tiene páginas.")
    chapters = outline(reader) if mode == "chapters" else []
    if chapters:
        starts = [page for _, page in chapters]
        ranges = [(title, page, (starts[i + 1] if i + 1 < len(starts) else total) - 1) for i, (title, page) in enumerate(chapters)]
        if starts[0] > 0:
            ranges.insert(0, ("Inicio", 0, starts[0] - 1))
    else:
        ranges = [(f"Páginas {s + 1}–{min(s + max_pages, total)}", s, min(s + max_pages, total) - 1) for s in range(0, total, max_pages)]

    chunks: list[Chunk] = []
    for title, start, end in ranges:
        parts, cursor = [], start
        while cursor <= end:
            scanned_here = any(len(texts[p]) < MIN_TEXT for p in range(cursor, min(cursor + max_pages, end + 1)))
            size = max(1, min(max_pages, max_scanned) if scanned_here else max_pages)
            parts.append((cursor, min(cursor + size, end + 1) - 1))
            cursor += size
        for number, (a, b) in enumerate(parts, start=1):
            pages = range(a, b + 1)
            body = "\n\n".join(f"[Página {p + 1}]\n{texts[p]}" for p in pages if texts[p])
            label = title if len(parts) == 1 else f"{title} (parte {number} de {len(parts)})"
            chunks.append(Chunk(label, a + 1, b + 1, body, [p + 1 for p in pages if len(texts[p]) < MIN_TEXT]))
    return chunks, bool(chapters)


def render_pages(data: bytes, pages: list[int], scale: float = 1.6) -> list[tuple[str, str, bytes]]:
    """Convierte páginas (desde 1) en PNG, para las que no tienen texto extraíble."""
    import pypdfium2 as pdfium

    document = pdfium.PdfDocument(data)
    images = []
    try:
        for number in pages:
            bitmap = document[number - 1].render(scale=scale)
            buffer = io.BytesIO()
            bitmap.to_pil().save(buffer, format="PNG", optimize=True)
            images.append((f"pagina-{number}.png", "image/png", buffer.getvalue()))
    finally:
        document.close()
    return images
