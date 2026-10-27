"""Manejo del formato de almacenamiento (storage format) de Confluence.

Las páginas se dividen en secciones por encabezados trabajando sobre el texto original,
sin re-serializar: al reemplazar una sección, el resto de la página queda byte a byte igual
(macros, tablas y CDATA incluidos).
"""

import html
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from html.entities import name2codepoint

from bs4 import BeautifulSoup

HEADING = re.compile(r"<h([1-6])(?:\s[^>]*)?>(.*?)</h\1\s*>", re.IGNORECASE | re.DOTALL)
LAYOUT_TAG = re.compile(r"</?ac:layout(?:-section|-cell)?(?:\s[^>]*)?>", re.IGNORECASE)
IMAGE_PLACEHOLDER = re.compile(r"\[\[IMAGEN:(\d+)\]\]")

NAMESPACES = {
    "ac": "http://atlassian.com/content",
    "ri": "http://atlassian.com/resource/identifier",
    "at": "http://atlassian.com/template",
}
XML_ENTITIES = {"amp", "lt", "gt", "quot", "apos"}


@dataclass
class Section:
    index: int
    start: int
    end: int
    heading: str
    level: int
    storage: str

    @property
    def text(self) -> str:
        return to_text(self.storage)


def to_text(storage: str) -> str:
    text = BeautifulSoup(storage, "html.parser").get_text(" ")
    return re.sub(r"\s+", " ", text).strip()


def split_sections(body: str) -> list[Section]:
    """Divide el cuerpo en secciones: cada encabezado abre una sección que termina en el
    siguiente encabezado o en un límite de layout (columnas), para no cortar estructuras."""
    regions: list[tuple[int, int]] = []
    cursor = 0
    for match in LAYOUT_TAG.finditer(body):
        regions.append((cursor, match.start()))
        cursor = match.end()
    regions.append((cursor, len(body)))

    sections: list[Section] = []
    last_heading, last_level = "", 0
    for region_start, region_end in regions:
        cuts = [m for m in HEADING.finditer(body, region_start, region_end)]
        starts = [region_start] + [m.start() for m in cuts]
        ends = [m.start() for m in cuts] + [region_end]
        for start, end in zip(starts, ends):
            chunk = body[start:end]
            if not to_text(chunk) and "<ac:image" not in chunk and "<ac:structured-macro" not in chunk:
                continue
            heading_match = HEADING.match(body, start, end)
            if heading_match:
                last_heading = to_text(heading_match.group(2)) or "(encabezado vacío)"
                last_level = int(heading_match.group(1))
                heading, level = last_heading, last_level
            else:
                heading = f"{last_heading} (cont.)" if last_heading else "(Introducción)"
                level = last_level
            sections.append(Section(len(sections), start, end, heading, level, chunk))
    return sections


def replace_section(body: str, section: Section, new_storage: str) -> str:
    return body[: section.start] + new_storage + body[section.end :]


def find_section(body: str, old_storage: str, preferred_index: int) -> Section | None:
    """Ubica una sección por su contenido exacto (la página pudo cambiar desde el análisis)."""
    sections = split_sections(body)
    if 0 <= preferred_index < len(sections) and sections[preferred_index].storage == old_storage:
        return sections[preferred_index]
    matches = [s for s in sections if s.storage == old_storage]
    return matches[0] if len(matches) == 1 else None


def validate_fragment(storage: str) -> None:
    """Comprueba que el fragmento sea XHTML bien formado; Confluence rechaza lo demás."""

    def entity(match: re.Match) -> str:
        name = match.group(1)
        if name in XML_ENTITIES or name not in name2codepoint:
            return match.group(0)
        return f"&#{name2codepoint[name]};"

    normalized = re.sub(r"&([a-zA-Z][a-zA-Z0-9]*);", entity, storage)
    declarations = " ".join(f'xmlns:{prefix}="{uri}"' for prefix, uri in NAMESPACES.items())
    try:
        ET.fromstring(f"<root {declarations}>{normalized}</root>")
    except ET.ParseError as error:
        raise ValueError(f"El contenido no es XHTML válido para Confluence: {error}") from error


def image_markup(filename: str) -> str:
    return f'<ac:image ac:width="640"><ri:attachment ri:filename="{html.escape(filename, quote=True)}" /></ac:image>'


def replace_placeholders(storage: str, replacement) -> str:
    """Sustituye [[IMAGEN:n]] usando replacement(n) -> str (n empieza en 1)."""
    return IMAGE_PLACEHOLDER.sub(lambda m: replacement(int(m.group(1))), storage)


def pretty(storage: str) -> list[str]:
    """Separa el storage en líneas por bloques para mostrar diferencias legibles."""
    spaced = re.sub(r"(</(?:p|h[1-6]|li|tr|table|ul|ol|blockquote|ac:[a-z-]+)>)", r"\1\n", storage)
    spaced = re.sub(r"(<(?:ul|ol|table|tbody)(?:\s[^>]*)?>)", r"\1\n", spaced)
    return [line for line in spaced.splitlines() if line.strip()]
