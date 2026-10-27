"""El "asesor" decide dónde va la información nueva y redacta el cambio.

Hay tres implementaciones con la misma interfaz:
- AnthropicAdvisor: usa Claude (texto e imágenes).
- OllamaAdvisor: usa un modelo local servido por Ollama; la información no sale de tu red.
- MockAdvisor: heurística local sin IA, para probar el flujo sin enviar datos a ningún servicio.
"""

import base64
import html

import httpx
from dataclasses import dataclass, field
from typing import Literal, Protocol

from pydantic import BaseModel, Field


class Change(BaseModel):
    action: Literal["update_section", "append_to_page", "create_page"] = Field(
        description="update_section: reemplaza una sección candidata; append_to_page: agrega al final de una página; "
        "create_page: crea una página hija de page_id."
    )
    page_id: str = Field(description="Página a modificar, o la página padre si action es create_page.")
    section_index: int = Field(description="Índice de la sección candidata para update_section; -1 en los demás casos.")
    new_page_title: str = Field(description="Título de la página nueva para create_page; vacío en los demás casos.")
    content_storage: str = Field(
        description="XHTML en storage format de Confluence. Para update_section, la sección COMPLETA ya actualizada "
        "(con su encabezado); para append_to_page, solo el bloque nuevo; para create_page, el cuerpo de la página."
    )
    summary: str = Field(description="Qué cambia, en una o dos frases, para el registro de versiones.")
    rationale: str = Field(description="Por qué esta ubicación es la adecuada.")
    confidence: float = Field(description="Confianza entre 0 y 1 en la ubicación y el contenido.")
    conflicts: list[str] = Field(
        description="Datos de la nota que contradicen lo ya documentado (qué decía antes y qué dice ahora). Vacío si no hay."
    )


class Alternative(BaseModel):
    page_id: str
    section_index: int = Field(description="-1 si la alternativa es la página completa.")
    reason: str


class Analysis(BaseModel):
    note_summary: str = Field(description="Resumen de una línea de la información recibida.")
    changes: list[Change] = Field(description="Cambios propuestos; vacío si la información ya está documentada.")
    alternatives: list[Alternative] = Field(description="Hasta 3 ubicaciones alternativas razonables.")
    no_change_reason: str = Field(description="Si changes está vacío, por qué. Vacío en otro caso.")


@dataclass
class Candidate:
    page_id: str
    page_title: str
    section_index: int
    heading: str
    storage: str
    score: float


@dataclass
class AdvisorInput:
    note_text: str
    source: str
    images: list[tuple[str, bytes]] = field(default_factory=list)
    candidates: list[Candidate] = field(default_factory=list)
    page_tree: str = ""
    hint: str = ""


class Advisor(Protocol):
    name: str

    def analyze(self, data: AdvisorInput) -> Analysis: ...


SYSTEM_PROMPT = """\
Eres el curador de la documentación de un proyecto en Confluence. El equipo desarrolla una simulación en realidad \
virtual del uso de un producto real; la documentación reúne lo que se sabe del producto (manuales de usuario, \
especificaciones) y del proyecto (decisiones, alcance, comportamiento esperado de la simulación, criterios de QA).

Recibirás información nueva que el productor copió de chats, tickets de Jira, manuales u otras fuentes, a veces con \
imágenes. Tu trabajo es decidir dónde va dentro del espacio de Confluence y redactar el cambio para que la \
documentación quede siempre en su versión más actualizada.

Cómo decidir:
- Prefiere actualizar una sección existente a crear contenido nuevo. Agrega al final de una página solo si ninguna \
sección encaja; crea una página nueva solo si el tema no tiene lugar en el espacio, eligiendo un padre lógico del árbol.
- Si la información nueva reemplaza algo documentado (un valor, un comportamiento, una decisión), sustituye el dato \
viejo en lugar de dejar ambos, y regístralo en conflicts.
- Si la información ya está documentada tal cual, no propongas cambios y explícalo en no_change_reason.
- Si la nota toca temas distintos, propón un cambio por cada lugar afectado.
- No inventes datos ni completes huecos con suposiciones; usa solo lo que dice la nota y lo que ya está en la página.
- Conserva el estilo, el idioma y la estructura de la página destino. Deja intacto todo lo que no cambia, incluidos \
macros (ac:structured-macro), imágenes y enlaces existentes, copiándolos sin modificar.
- Ve la diferencia entre la realidad del producto y lo que decide el proyecto para la simulación; si la nota no deja \
claro de cuál se trata, dilo en rationale y baja la confianza.
- Cuando valga la pena citar la fuente (por ejemplo un ticket o un manual), menciónala brevemente en el texto.

Formato del contenido (content_storage): XHTML bien formado del storage format de Confluence. Usa <h2>/<h3>, <p>, \
<ul>/<ol>/<li>, <strong>, <em>, <code>, <table><tbody><tr><th>/<td>, <a href="...">. Escapa &, < y > en el texto. \
Para insertar una imagen de la nota escribe el marcador [[IMAGEN:n]] (n empieza en 1) donde deba ir; la herramienta \
la sube como adjunto. Incluye una imagen solo si aporta a la documentación.
"""


def render_candidates(candidates: list[Candidate]) -> str:
    if not candidates:
        return "(No hay secciones candidatas: el espacio está vacío o nada coincide.)"
    blocks = []
    for c in candidates:
        blocks.append(
            f'<candidate page_id="{c.page_id}" page_title="{html.escape(c.page_title)}" '
            f'section_index="{c.section_index}" heading="{html.escape(c.heading)}">\n{c.storage}\n</candidate>'
        )
    return "\n\n".join(blocks)


def build_prompt(data: AdvisorInput) -> str:
    parts = [
        "<page_tree>\n" + (data.page_tree or "(espacio vacío)") + "\n</page_tree>",
        "<candidates>\n" + render_candidates(data.candidates) + "\n</candidates>",
        f"<note source=\"{html.escape(data.source)}\" images=\"{len(data.images)}\">\n{data.note_text}\n</note>",
    ]
    if data.hint:
        parts.append(f"<user_hint>\n{data.hint}\n</user_hint>\nRespeta la indicación del usuario sobre la ubicación.")
    parts.append(
        "Decide dónde va la información de la nota y redacta los cambios. Para update_section usa solo secciones "
        "candidatas (page_id + section_index)."
    )
    return "\n\n".join(parts)


class AnthropicAdvisor:
    name = "anthropic"

    def __init__(self, model: str, effort: str, client=None):
        import anthropic

        self.client = client or anthropic.Anthropic()
        self.model = model
        self.effort = effort

    def analyze(self, data: AdvisorInput) -> Analysis:
        content: list[dict] = []
        for mime, raw in data.images:
            content.append(
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": mime, "data": base64.standard_b64encode(raw).decode()},
                }
            )
        content.append({"type": "text", "text": build_prompt(data)})
        # Si los filtros de seguridad rechazan la solicitud, la API la reintenta con el modelo de respaldo.
        response = self.client.beta.messages.parse(
            model=self.model,
            max_tokens=32000,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": content}],
            output_config={"effort": self.effort},
            output_format=Analysis,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
        if response.stop_reason == "refusal":
            raise RuntimeError("El modelo rechazó analizar esta nota.")
        if response.stop_reason == "max_tokens":
            raise RuntimeError("La respuesta se cortó por longitud; divide la nota en partes más pequeñas.")
        if response.parsed_output is None:
            raise RuntimeError("El modelo no devolvió un análisis válido.")
        return response.parsed_output


def inline_refs(schema: dict) -> dict:
    """Expande los $ref de Pydantic: algunos servidores locales no resuelven $defs al restringir la salida."""
    definitions = schema.get("$defs", {})

    def expand(node):
        if isinstance(node, dict):
            if "$ref" in node:
                return expand(definitions[node["$ref"].rsplit("/", 1)[-1]])
            return {k: expand(v) for k, v in node.items() if k != "$defs"}
        if isinstance(node, list):
            return [expand(v) for v in node]
        return node

    return expand(schema)


class OllamaAdvisor:
    """Modelo local con Ollama (https://ollama.com). Para analizar imágenes, el modelo debe tener visión."""

    name = "ollama"

    def __init__(self, base_url: str, model: str, context: int, transport: httpx.BaseTransport | None = None):
        self.model = model
        self.context = context
        self.http = httpx.Client(base_url=base_url.rstrip("/"), timeout=900, transport=transport)

    def analyze(self, data: AdvisorInput) -> Analysis:
        user = {"role": "user", "content": build_prompt(data)}
        if data.images:
            user["images"] = [base64.standard_b64encode(raw).decode() for _, raw in data.images]
        payload = {
            "model": self.model,
            "stream": False,
            "format": inline_refs(Analysis.model_json_schema()),
            "options": {"temperature": 0, "num_ctx": self.context},
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT + "\nResponde solo con el JSON pedido."},
                user,
            ],
        }
        try:
            response = self.http.post("/api/chat", json=payload)
        except httpx.HTTPError as error:
            raise RuntimeError(f"No se pudo conectar con Ollama en {self.http.base_url}: {error}") from error
        if not response.is_success:
            raise RuntimeError(f"Ollama respondió {response.status_code}: {response.text[:300]}")
        body = response.json()
        if body.get("done_reason") == "length":
            raise RuntimeError("La respuesta se cortó por longitud; aumenta OLLAMA_CONTEXT o divide la nota.")
        try:
            return Analysis.model_validate_json(body["message"]["content"])
        except (KeyError, ValueError) as error:
            raise RuntimeError(f"El modelo local no devolvió un análisis válido: {error}") from error


class MockAdvisor:
    """Sin IA: agrega la nota a la sección con mejor coincidencia. Sirve para probar el flujo de punta a punta."""

    name = "mock"

    def analyze(self, data: AdvisorInput) -> Analysis:
        note_html = "".join(f"<p>{html.escape(line)}</p>" for line in data.note_text.splitlines() if line.strip())
        note_html += "".join(f"<p>[[IMAGEN:{i}]]</p>" for i in range(1, len(data.images) + 1))
        if data.source:
            note_html += f"<p><em>Fuente: {html.escape(data.source)}</em></p>"
        alternatives = [
            Alternative(page_id=c.page_id, section_index=c.section_index, reason=f"Coincidencia con «{c.heading}»")
            for c in data.candidates[1:4]
        ]
        if data.candidates:
            best = data.candidates[0]
            change = Change(
                action="update_section",
                page_id=best.page_id,
                section_index=best.section_index,
                new_page_title="",
                content_storage=best.storage + note_html,
                summary=f"Agrega información a «{best.heading}»",
                rationale=f"Sección con más palabras en común con la nota (modo sin IA, puntaje {best.score:.1f}).",
                confidence=0.3,
                conflicts=[],
            )
        else:
            change = Change(
                action="create_page",
                page_id="",
                section_index=-1,
                new_page_title=(data.note_text.strip().splitlines() or ["Nota nueva"])[0][:80],
                content_storage=note_html,
                summary="Crea una página con la nota",
                rationale="No hay secciones parecidas en el espacio (modo sin IA).",
                confidence=0.2,
                conflicts=[],
            )
        return Analysis(
            note_summary=data.note_text.strip()[:120],
            changes=[change],
            alternatives=alternatives,
            no_change_reason="",
        )
