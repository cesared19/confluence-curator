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
