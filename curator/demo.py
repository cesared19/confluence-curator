"""Confluence simulado en memoria (CURATOR_DEMO=1) para probar la herramienta sin credenciales."""

import itertools

from .confluence import ConfluenceError, Page

SAMPLE = [
    ("1", "Simulador VR - Inicio", None, "<p>Documentación del simulador de entrenamiento en realidad virtual.</p>"),
    (
        "2",
        "Producto real: manual de referencia",
        "1",
        "<h2>Encendido</h2><p>El equipo se enciende manteniendo presionado el botón POWER durante 3 segundos.</p>"
        "<h2>Panel de control</h2><p>El panel tiene cuatro perillas: presión, temperatura, flujo y modo.</p>"
        "<h2>Alarmas</h2><p>La alarma de sobrepresión suena a partir de 8 bar.</p>",
    ),
    (
        "3",
        "Simulación: comportamiento esperado",
        "1",
        "<h2>Interacciones</h2><p>El usuario toma las perillas con el gatillo del control y las gira con la muñeca.</p>"
        "<h2>Alarmas en la simulación</h2><p>Las alarmas se reproducen con sonido espacial desde el panel.</p>",
    ),
    (
        "4",
        "Criterios de QA",
        "1",
        "<h2>Rendimiento</h2><p>La simulación debe mantener 72 FPS en Quest 3.</p>"
        "<h2>Accesibilidad</h2><p>Todos los textos deben leerse a 1,5 m de distancia.</p>",
    ),
]


class FakeConfluence:
    base_url = "https://demo.atlassian.net"

    def __init__(self):
        self.pages = {pid: Page(pid, title, parent, 1, body) for pid, title, parent, body in SAMPLE}
        self.attachments: dict[str, dict[str, bytes]] = {}
        self.ids = itertools.count(100)

    def page_url(self, page_id: str) -> str:
        return f"/demo/pages/{page_id}"

    def space_id(self, space_key: str) -> str:
        return "demo"

    def list_pages(self, space_id: str) -> list[Page]:
        return list(self.pages.values())

    def get_page(self, page_id: str) -> Page:
        if page_id not in self.pages:
            raise ConfluenceError("Página no encontrada", 404)
        return self.pages[page_id]

    def update_page(self, page: Page, body: str, message: str) -> Page:
        current = self.get_page(page.id)
        if current.version != page.version:
            raise ConfluenceError("Conflicto de versión", 409)
        self.pages[page.id] = Page(page.id, page.title, page.parent_id, page.version + 1, body)
        return self.pages[page.id]

    def create_page(self, space_id: str, parent_id: str | None, title: str, body: str) -> Page:
        page = Page(str(next(self.ids)), title, parent_id, 1, body)
        self.pages[page.id] = page
        return page

    def upload_attachment(self, page_id: str, filename: str, content: bytes, mime: str, comment: str) -> None:
        self.attachments.setdefault(page_id, {})[filename] = content


DEMO_ISSUES = {
    "SIM-101": (
        "Actualizar umbral de alarma de sobrepresión",
        "Bug",
        "En curso",
        "2026-10-20T10:00:00.000-0500",
        "Según el manual del fabricante v2.3, la alarma de sobrepresión ahora suena a partir de 7,5 bar (antes 8 bar). "
        "La simulación debe reflejar el nuevo umbral.",
        [("Ana Pérez", "2026-10-21T09:00:00.000-0500", "Confirmado con el cliente: aplica a todos los modelos 2026.")],
    ),
    "SIM-102": (
        "Agregar perilla de volumen al panel",
        "Historia",
        "Por hacer",
        "2026-10-22T16:30:00.000-0500",
        "El panel real tiene una quinta perilla de volumen para la alarma. Hay que modelarla e incluirla en la interacción.",
        [],
    ),
    "SIM-103": (
        "Revisar rendimiento en Quest 3",
        "Tarea",
        "Hecho",
        "2026-10-18T12:00:00.000-0500",
        "Se acordó bajar el objetivo a 72 FPS estables con picos tolerados de 60 FPS durante la carga de escena.",
        [("Luis Gómez", "2026-10-18T12:00:00.000-0500", "QA valida con OpenVR Metrics durante 10 minutos de sesión.")],
    ),
}


class FakeJira:
    """Jira simulado con tickets de ejemplo para el modo demo."""

    def __init__(self):
        self.issues = {key: list(value) for key, value in DEMO_ISSUES.items()}
        self.queries: list[str] = []

    def touch(self, key: str, updated: str, comment: tuple[str, str] | None = None) -> None:
        """Simula un cambio en un ticket (para pruebas y capturas)."""
        self.issues[key][3] = updated
        if comment:
            self.issues[key][5] = self.issues[key][5] + [(comment[0], updated, comment[1])]

    def issue_url(self, key: str) -> str:
        return f"https://demo.atlassian.net/browse/{key}"

    def search(self, query: str, limit: int = 50):
        from .jira import IssueSummary, to_jql

        self.queries.append(query)
        jql = to_jql(query).upper()
        keys = [k for k in self.issues if k in jql] or list(self.issues)
        return [
            IssueSummary(k, self.issues[k][0], self.issues[k][2], self.issues[k][1], self.issues[k][3]) for k in keys
        ][:limit]

    def issue(self, key: str, max_images: int, max_image_bytes: int):
        from .jira import Issue

        if key not in self.issues:
            raise ConfluenceError(f"Jira respondió 404: no existe {key}", 404)
        summary, kind, status, updated, description, comments = self.issues[key]
        lines = [f"{key}: {summary}", f"Tipo: {kind}", f"Estado: {status}", "", "Descripción:", description]
        if comments:
            lines += ["", "Comentarios:"] + [f"- {who} ({when[:10]}): {text}" for who, when, text in comments]
        return Issue(key, self.issue_url(key), updated, "\n".join(lines))
