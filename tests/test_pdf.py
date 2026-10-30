import io

from fastapi.testclient import TestClient
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

from curator import pdf
from curator.advisor import MockAdvisor
from curator.config import Settings
from curator.db import Database
from curator.demo import FakeConfluence
from curator.service import Curator, CuratorError
from curator.web import create_app

CHAPTERS = [
    ("Encendido", ["El equipo se enciende con el botón POWER durante 3 segundos.", "El indicador verde confirma el arranque."]),
    ("Panel de control", ["El panel tiene cinco perillas: presión, temperatura, flujo, modo y volumen.", "La perilla de modo tiene tres posiciones."]),
    ("Alarmas", ["La alarma de sobrepresión suena a partir de 7,5 bar.", "Se silencia girando la perilla de modo."]),
]


def make_pdf(outline: str = "nested", scanned_page: bool = False, title: str = "Manual X-200 v2.3", pages_per_chapter: int = 2) -> bytes:
    """PDF de prueba: encabezado y pie repetidos, un capítulo por bloque de páginas y, opcionalmente, marcadores."""
    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=A4)
    c.setTitle(title)
    number = 0
    if outline == "nested":
        c.bookmarkPage("root")
        c.addOutlineEntry("Manual X-200", "root", level=0)
    for chapter, (name, lines) in enumerate(CHAPTERS):
        for page in range(pages_per_chapter):
            number += 1
            if page == 0 and outline != "none":
                key = f"ch{chapter}"
                c.bookmarkPage(key)
                c.addOutlineEntry(name, key, level=1 if outline == "nested" else 0)
            if scanned_page and chapter == 1 and page == 1:
                c.rect(100, 300, 300, 300, fill=1)  # página sin texto: un dibujo
            else:
                c.drawString(72, 800, "Fabricante ACME - Manual X-200 - Confidencial")
                c.drawString(72, 760, f"{name}")
                c.drawString(72, 740, f"Esta parte del texto conti-")
                c.drawString(72, 725, f"núa en la línea siguiente ({number}).")
                y = 700
                for line in lines:
                    c.drawString(72, y, f"{line} (página {page + 1} del capítulo)")
                    y -= 20
                c.drawString(72, 40, f"Página {number} de {len(CHAPTERS) * pages_per_chapter}")
            c.showPage()
    c.save()
    return buffer.getvalue()


def test_outline_chapters_nested_and_flat():
    for kind in ("nested", "flat"):
        chunks, chapters = pdf.plan(make_pdf(outline=kind), "chapters", 8, 5)
        assert chapters
        assert [(c.title, c.start, c.end) for c in chunks] == [
            ("Encendido", 1, 2), ("Panel de control", 3, 4), ("Alarmas", 5, 6)
        ]


def test_text_cleanup():
    chunks, _ = pdf.plan(make_pdf(), "chapters", 8, 5)
    text = chunks[2].text
    assert "[Página 5]" in text and "[Página 6]" in text
    assert "La alarma de sobrepresión suena a partir de 7,5 bar." in text
    assert "Confidencial" not in text and "Página 5 de 6" not in text
    assert "continúa en la línea siguiente" in text


def test_page_blocks_when_no_outline_or_requested():
    chunks, chapters = pdf.plan(make_pdf(outline="none"), "chapters", 4, 5)
    assert not chapters
    assert [(c.title, c.start, c.end) for c in chunks] == [("Páginas 1–4", 1, 4), ("Páginas 5–6", 5, 6)]
    chunks, chapters = pdf.plan(make_pdf(), "pages", 3, 5)
    assert not chapters and [c.key for c in chunks] == ["1-3", "4-6"]


def test_long_chapters_are_split():
    chunks, _ = pdf.plan(make_pdf(pages_per_chapter=5), "chapters", 2, 5)
    assert [(c.title, c.key) for c in chunks[:3]] == [
        ("Encendido (parte 1 de 3)", "1-2"), ("Encendido (parte 2 de 3)", "3-4"), ("Encendido (parte 3 de 3)", "5-5")
    ]


def test_scanned_pages_are_detected_rendered_and_limited():
    chunks, _ = pdf.plan(make_pdf(scanned_page=True), "chapters", 8, 1)
    panel = [c for c in chunks if c.title.startswith("Panel de control")]
    assert [(c.key, c.scanned) for c in panel] == [("3-3", []), ("4-4", [4])]
    [(name, mime, data)] = pdf.render_pages(make_pdf(scanned_page=True), [4])
    assert (name, mime) == ("pagina-4.png", "image/png") and data.startswith(b"\x89PNG")


def test_invalid_pdf():
    try:
        pdf.plan(b"%PDF-1.4 roto", "chapters", 8, 5)
        raise AssertionError("debía fallar")
    except pdf.PdfError as error:
        assert "No se pudo leer" in str(error)


def make_curator() -> Curator:
    curator = Curator(Database(":memory:"), MockAdvisor(), FakeConfluence(), "DEMO", background=False)
    curator.sync()
    return curator


def test_import_pdf_chunks():
    curator = make_curator()
    document_id = curator.add_pdf("x200.pdf", make_pdf(scanned_page=True), "Manual del producto")
    document = curator.pdf_document(document_id)
    assert (document["title"], document["pages"]) == ("Manual X-200 v2.3", 6)

    chunks, chapters = curator.pdf_plan(document_id)
    assert chapters and curator.db.one("SELECT plan_params FROM pdf_documents")["plan_params"]
    note_ids = curator.import_pdf_chunks(document_id, ["3-4", "5-6"], "chapters", 8)
    assert len(note_ids) == 2

    panel = curator.note(note_ids[0])
    assert panel["source_type"] == "Manual del producto"
    assert panel["source_ref"] == "Manual X-200 v2.3, p. 3–4"
    assert panel["text"].startswith("Manual X-200 v2.3: Panel de control (páginas 3–4)\nLa página 4 no tiene texto extraíble y se adjunta como imagen.")
    [image] = curator.note_images(note_ids[0])
    assert image["filename"] == "pagina-4.png"
    assert curator.proposals(note_ids[1])[0]["page_id"] == "2"  # Alarmas va al manual de referencia
    assert curator.pdf_imported(document_id) == {"3-4": note_ids[0], "5-6": note_ids[1]}
    assert curator.pdf_documents()[0]["imported"] == 2


def test_add_pdf_rejects_broken_file():
    curator = make_curator()
    try:
        curator.add_pdf("roto.pdf", b"%PDF-1.4 nada", "Manual del producto")
        raise AssertionError("debía fallar")
    except CuratorError as error:
        assert "No se pudo leer" in str(error)
    try:
        curator.pdf_plan(999)
        raise AssertionError("debía fallar")
    except CuratorError:
        pass


def test_pdf_web_flow():
    curator = make_curator()
    client = TestClient(create_app(curator, Settings()))
    uploaded = client.post("/pdf", files={"file": ("x200.pdf", make_pdf(), "application/pdf")}, data={"source_type": "Manual del producto"})
    assert uploaded.url.path == "/pdf/1"
    assert "3 partes" in uploaded.text and "Panel de control" in uploaded.text

    blocks = client.get("/pdf/1", params={"mode": "pages", "max_pages": 2})
    assert "Páginas 1–2" in blocks.text

    one = client.post("/pdf/1/import", data={"keys": ["5-6"], "mode": "chapters", "max_pages": "8"})
    assert one.url.path == "/" and "Importando 1 parte de «Manual X-200 v2.3»" in one.text
    assert "Importada" in client.get("/pdf/1").text
    assert "Manual X-200 v2.3" in client.get("/pdf").text

    assert "Selecciona al menos" in client.post("/pdf/1/import", data={"mode": "chapters", "max_pages": "8"}).text
    assert "no es un PDF" in client.post("/pdf", files={"file": ("a.pdf", b"hola", "application/pdf")}).text
    assert "entre 1 y 50" in client.get("/pdf/1", params={"max_pages": 0}).text
