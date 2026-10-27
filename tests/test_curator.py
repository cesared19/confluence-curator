import pytest

from curator.advisor import Analysis, Change, MockAdvisor
from curator.db import Database
from curator.demo import FakeConfluence
from curator.search import BM25, Document, tokenize
from curator.service import Curator, CuratorError


class ScriptedAdvisor:
    name = "scripted"

    def __init__(self, analysis: Analysis):
        self.analysis = analysis
        self.inputs = []

    def analyze(self, data):
        self.inputs.append(data)
        return self.analysis


def change(**kwargs) -> Change:
    defaults = dict(
        action="update_section",
        page_id="2",
        section_index=2,
        new_page_title="",
        content_storage="<h2>Alarmas</h2><p>La alarma suena a partir de 7,5 bar.</p><p>[[IMAGEN:1]]</p>",
        summary="Umbral de sobrepresión a 7,5 bar",
        rationale="Es la sección de alarmas del manual",
        confidence=0.9,
        conflicts=["Antes 8 bar, ahora 7,5 bar"],
    )
    return Change(**{**defaults, **kwargs})


def make(advisor) -> tuple[Curator, FakeConfluence]:
    confluence = FakeConfluence()
    curator = Curator(Database(":memory:"), advisor, confluence, "DEMO")
    curator.sync()
    return curator, confluence


def analysis(*changes) -> Analysis:
    return Analysis(note_summary="Nuevo umbral", changes=list(changes), alternatives=[], no_change_reason="")


def test_tokenize_folds_accents_and_stopwords():
    assert tokenize("La Alarma de SOBREPRESIÓN") == ["alarma", "sobrepresion"]


def test_bm25_ranks_relevant_first():
    docs = [Document("a", "rendimiento fps quest"), Document("b", "alarma sobrepresion bar")]
    assert BM25(docs).search("sobrepresión alarma", 5)[0][0].key == "b"


def test_search_finds_section():
    curator, _ = make(MockAdvisor())
    results = curator.search("alarma sobrepresión bar")
    assert (results[0].page_title, results[0].heading) == ("Producto real: manual de referencia", "Alarmas")


def test_analyze_and_apply_update_with_image():
    advisor = ScriptedAdvisor(analysis(change()))
    curator, confluence = make(advisor)
    note_id = curator.create_note("Ahora suena a 7,5 bar", "Jira", "SIM-12", [("foto.png", "image/png", b"png")])
    curator.analyze(note_id)

    sent = advisor.inputs[0]
    assert sent.source == "Jira SIM-12"
    assert sent.images == [("image/png", b"png")]
    assert "[2] Producto real: manual de referencia" in sent.page_tree

    [proposal] = curator.proposals(note_id)
    assert proposal["old_storage"] == "<h2>Alarmas</h2><p>La alarma de sobrepresión suena a partir de 8 bar.</p>"
    url = curator.apply(proposal["id"], proposal["new_storage"])

    page = confluence.pages["2"]
    assert page.version == 2
    assert page.body.startswith("<h2>Encendido</h2>")
    assert "7,5 bar" in page.body and "8 bar" not in page.body
    assert 'ri:filename="nota-1-imagen-1.png"' in page.body
    assert confluence.attachments["2"] == {"nota-1-imagen-1.png": b"png"}
    assert url == "/demo/pages/2"
    assert curator.proposal(proposal["id"])["status"] == "applied"
    assert curator.page("2").version == 2


def test_apply_detects_section_changed_in_confluence():
    curator, confluence = make(ScriptedAdvisor(analysis(change())))
    note_id = curator.create_note("7,5 bar", "Chat", "", [("foto.png", "image/png", b"png")])
    curator.analyze(note_id)
    page = confluence.pages["2"]
    confluence.update_page(page, page.body.replace("8 bar", "9 bar"), "edición manual")

    [proposal] = curator.proposals(note_id)
    with pytest.raises(CuratorError, match="cambió en Confluence"):
        curator.apply(proposal["id"], proposal["new_storage"])
    assert curator.proposal(proposal["id"])["status"] == "pending"


def test_apply_after_unrelated_edit_still_works():
    curator, confluence = make(ScriptedAdvisor(analysis(change())))
    note_id = curator.create_note("7,5 bar", "Chat", "", [])
    curator.analyze(note_id)
    page = confluence.pages["2"]
    confluence.update_page(page, "<p>Aviso nuevo</p>" + page.body, "edición manual")

    [proposal] = curator.proposals(note_id)
    curator.apply(proposal["id"], proposal["new_storage"].replace("<p>[[IMAGEN:1]]</p>", ""))
    body = confluence.pages["2"].body
    assert body.startswith("<p>Aviso nuevo</p><h2>Encendido</h2>") and body.endswith("7,5 bar.</p>")


def test_create_page_and_append():
    curator, confluence = make(
        ScriptedAdvisor(
            analysis(
                change(action="create_page", page_id="1", section_index=-1, new_page_title="Glosario",
                       content_storage="<p>Bar: unidad de presión.</p>"),
                change(action="append_to_page", page_id="4", section_index=-1,
                       content_storage="<h2>Audio</h2><p>Las alarmas deben oírse sobre el ruido ambiente.</p>"),
            )
        )
    )
    note_id = curator.create_note("Glosario y audio", "Reunión", "", [])
    curator.analyze(note_id)
    created, appended = curator.proposals(note_id)

    curator.apply(created["id"], created["new_storage"], "Glosario del producto")
    new_page = next(p for p in confluence.pages.values() if p.title == "Glosario del producto")
    assert new_page.parent_id == "1"

    curator.apply(appended["id"], appended["new_storage"])
    assert confluence.pages["4"].body.endswith("<h2>Audio</h2><p>Las alarmas deben oírse sobre el ruido ambiente.</p>")


def test_invalid_proposals_are_flagged():
    curator, _ = make(
        ScriptedAdvisor(
            analysis(change(section_index=99), change(content_storage="<p>sin cerrar"))
        )
    )
    note_id = curator.create_note("x", "Chat", "", [])
    curator.analyze(note_id)
    missing, broken = curator.proposals(note_id)
    assert "no existe" in missing["error"]
    assert "XHTML" in broken["error"]
    with pytest.raises(ValueError):
        curator.apply(broken["id"], broken["new_storage"])


def test_reanalyze_with_target_supersedes_previous():
    advisor = ScriptedAdvisor(analysis(change()))
    curator, _ = make(advisor)
    note_id = curator.create_note("7,5 bar", "Chat", "", [])
    curator.analyze(note_id)
    curator.analyze(note_id, "es de la simulación", target_page="3", target_section=1)

    hint_input = advisor.inputs[1]
    assert hint_input.candidates[0].page_id == "3" and hint_input.candidates[0].section_index == 1
    assert "Alarmas en la simulación" in hint_input.hint and "es de la simulación" in hint_input.hint
    statuses = [r["status"] for r in curator.db.all("SELECT status FROM proposals ORDER BY id")]
    assert statuses == ["superseded", "pending"]


def test_advisor_failure_is_recorded():
    class Failing:
        name = "failing"

        def analyze(self, data):
            raise RuntimeError("sin conexión")

    curator, _ = make(Failing())
    note_id = curator.create_note("x", "Chat", "", [])
    curator.analyze(note_id)
    assert "sin conexión" in curator.note(note_id)["error"]


def test_mock_advisor_end_to_end():
    curator, confluence = make(MockAdvisor())
    note_id = curator.create_note("Las perillas del panel ahora son cinco: se agregó volumen", "Manual del producto", "", [])
    curator.analyze(note_id)
    [proposal] = curator.proposals(note_id)
    assert proposal["action"] == "update_section" and proposal["error"] == ""
    curator.apply(proposal["id"], proposal["new_storage"])
    assert "se agregó volumen" in confluence.pages["2"].body


def test_empty_note_rejected():
    curator, _ = make(MockAdvisor())
    with pytest.raises(CuratorError):
        curator.create_note("  ", "Chat", "", [])
