"""Servidor de demostración para generar las capturas del README.

Usa el Confluence y el Jira simulados y un asesor con propuestas de ejemplo escritas a mano (no las genera
un modelo), para que las capturas muestren cómo se ven las propuestas sin necesitar credenciales ni API key.

    python scripts/demo_server.py --port 8770
"""

import argparse
import sys
from pathlib import Path

import uvicorn

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from curator.advisor import Alternative, Analysis, Change, ContradictionCheck, MockAdvisor  # noqa: E402
from curator.config import Settings  # noqa: E402
from curator.db import Database  # noqa: E402
from curator.demo import FakeConfluence, FakeJira  # noqa: E402
from curator.service import Curator  # noqa: E402
from curator.web import create_app  # noqa: E402

EXAMPLES = {
    "SIM-101": dict(
        heading="Alarmas",
        old="La alarma de sobrepresión suena a partir de 8 bar.",
        new="La alarma de sobrepresión suena a partir de 7,5 bar (manual del fabricante v2.3; aplica a todos los modelos 2026, SIM-101).",
        summary="Umbral de la alarma de sobrepresión: de 8 a 7,5 bar",
        rationale="La sección Alarmas del manual de referencia documenta este umbral; el ticket cita la versión 2.3 del manual y la confirmación del cliente.",
        confidence=0.92,
        conflicts=["La página dice 8 bar; el ticket SIM-101 (manual v2.3) dice 7,5 bar."],
        alternatives=[("3", 1, "Si además cambia cómo suena en la simulación, va en «Alarmas en la simulación».")],
    ),
    "SIM-102": dict(
        heading="Panel de control",
        old="El panel tiene cuatro perillas: presión, temperatura, flujo y modo.",
        new="El panel tiene cinco perillas: presión, temperatura, flujo, modo y volumen de la alarma (SIM-102).",
        summary="El panel tiene una quinta perilla: volumen de la alarma",
        rationale="Describe el hardware real del producto, que está documentado en «Panel de control» del manual de referencia.",
        confidence=0.86,
        conflicts=["La página dice cuatro perillas; el ticket SIM-102 dice cinco."],
        alternatives=[("3", 0, "La interacción con la nueva perilla podría documentarse en «Interacciones».")],
    ),
    "SIM-103": dict(
        heading="Rendimiento",
        old="La simulación debe mantener 72 FPS en Quest 3.",
        new="La simulación debe mantener 72 FPS estables en Quest 3; se toleran picos de 60 FPS durante la carga de escena. "
        "QA lo valida con OpenVR Metrics en sesiones de 10 minutos (SIM-103).",
        summary="Criterio de rendimiento: picos tolerados y método de validación",
        rationale="Es un criterio de aceptación de la simulación, que vive en «Criterios de QA › Rendimiento».",
        confidence=0.88,
        conflicts=[],
        alternatives=[],
    ),
    "botón B": dict(
        heading="Alarmas en la simulación",
        old="Las alarmas se reproducen con sonido espacial desde el panel.",
        new="Las alarmas se reproducen con sonido espacial desde el panel. El usuario las silencia con el botón B del control "
        "derecho; en el equipo real se silencian desde la perilla de modo (decisión de la reunión de diseño del 24/10).",
        summary="Cómo se silencian las alarmas en la simulación",
        rationale="Es una decisión del proyecto sobre la simulación, no del producto real; la nota aclara la diferencia con el equipo.",
        confidence=0.81,
        conflicts=[],
        alternatives=[("2", 2, "La forma de silenciarlas en el equipo real podría ir en «Alarmas» del manual de referencia.")],
    ),
}


class ExampleAdvisor:
    """Propuestas de ejemplo para la demostración; cualquier otra nota usa el modo sin IA."""

    name = "example"
    can_compare = True

    def compare(self, a, b):
        texts = {ref.heading: ref.storage for ref in (a, b)}
        manual, simulation = texts.get("Alarmas", ""), texts.get("Alarmas en la simulación", "")
        if "8 bar" in manual and "7 bar" in simulation:
            return ContradictionCheck(
                contradicts=True,
                explanation="El manual de referencia dice que la alarma de sobrepresión suena a partir de 8 bar; la página "
                "de la simulación dice que se activa a los 7 bar, sin indicar que la diferencia sea intencional.",
                outdated="b" if b.heading == "Alarmas en la simulación" else "a",
                confidence=0.87,
                suggestion="Alinear la simulación con el umbral vigente del producto, o aclarar que la diferencia es una "
                "decisión del proyecto.",
            )
        return ContradictionCheck(contradicts=False, explanation="", outdated="unknown", confidence=0.05, suggestion="")

    def analyze(self, data):
        for marker, example in EXAMPLES.items():
            if marker not in data.note_text:
                continue
            candidate = next((c for c in data.candidates if c.heading == example["heading"]), None)
            if candidate is None or example["old"] not in candidate.storage:
                break
            return Analysis(
                note_summary=example["summary"],
                changes=[
                    Change(
                        action="update_section",
                        page_id=candidate.page_id,
                        section_index=candidate.section_index,
                        new_page_title="",
                        content_storage=candidate.storage.replace(example["old"], example["new"]),
                        summary=example["summary"],
                        rationale=example["rationale"],
                        confidence=example["confidence"],
                        conflicts=example["conflicts"],
                    )
                ],
                alternatives=[Alternative(page_id=p, section_index=i, reason=r) for p, i, r in example["alternatives"]],
                no_change_reason="",
            )
        return MockAdvisor().analyze(data)


def sample_manual() -> bytes:
    """Manual de ejemplo con marcadores de capítulos y una página escaneada (requiere reportlab)."""
    import io

    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    chapters = [
        ("1. Seguridad", ["Use guantes y lentes de protección durante la operación.", "No opere el equipo con la tapa abierta."]),
        ("2. Encendido y apagado", ["Mantenga presionado POWER durante 3 segundos.", "El indicador verde confirma el arranque."]),
        ("3. Panel de control", ["El panel tiene cinco perillas: presión, temperatura, flujo, modo y volumen.", "La perilla de modo tiene tres posiciones: manual, automático y mantenimiento."]),
        ("4. Alarmas", ["La alarma de sobrepresión suena a partir de 7,5 bar.", "Gire la perilla de modo a mantenimiento para silenciarla."]),
        ("5. Mantenimiento", ["Revise los sellos cada 500 horas de uso.", "Reemplace el filtro cada 1.000 horas."]),
    ]
    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=A4)
    c.setTitle("Manual de operación X-200 v2.3")
    number = 0
    for index, (name, lines) in enumerate(chapters):
        for page in range(3):
            number += 1
            if page == 0:
                c.bookmarkPage(f"c{index}")
                c.addOutlineEntry(name, f"c{index}", level=0)
            if index == 2 and page == 2:
                c.setFillGray(0.85)
                c.rect(90, 250, 410, 420, fill=1, stroke=0)
                c.setFillGray(0.3)
                for knob in range(5):
                    c.circle(140 + knob * 80, 460, 28, fill=1)
                c.setFillGray(0)
            else:
                c.setFont("Helvetica", 9)
                c.drawString(72, 805, "ACME Industrial - Manual de operación X-200 v2.3")
                c.setFont("Helvetica-Bold", 14)
                c.drawString(72, 760, name)
                c.setFont("Helvetica", 11)
                y = 730
                for line in lines:
                    c.drawString(72, y, line if page == 0 else f"{line} (detalle {page})")
                    y -= 18
                c.setFont("Helvetica", 9)
                c.drawString(72, 40, f"Página {number}")
            c.showPage()
    c.save()
    return buffer.getvalue()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8770)
    args = parser.parse_args()
    curator = Curator(Database(":memory:"), ExampleAdvisor(), FakeConfluence(), "DEMO", jira=FakeJira())
    try:
        curator.add_pdf("manual-x200-v2.3.pdf", sample_manual(), "Manual del producto")
    except ImportError:
        print("reportlab no está instalado: el demo arranca sin el manual de ejemplo.")
    uvicorn.run(create_app(curator, Settings()), host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
