import threading

from fastapi.testclient import TestClient

from curator.activity import Activity
from curator.advisor import ContradictionCheck, MockAdvisor
from curator.config import Settings
from curator.db import Database
from curator.demo import FakeConfluence, FakeJira
from curator.service import Curator
from curator.web import create_app


def test_activity_task_lifecycle():
    activity = Activity()
    with activity.task("import", "Importando") as task:
        task.update(detail="SIM-1", done=1, total=3)
        assert activity.snapshot()["tasks"][0] == {
            "id": 1, "kind": "import", "label": "Importando", "detail": "SIM-1", "done": 1, "total": 3,
            "started": task.started, "link": "", "stopping": False,
        }
        task.result = "Listo"
    assert activity.snapshot()["tasks"] == [] and activity.events[0]["text"] == "Listo"

    with activity.task("audit", "Revisión") as task:
        assert activity.stop() == 1
        assert activity.snapshot()["tasks"][0]["stopping"] is True  # se muestra como «Deteniendo» hasta su punto de control
        task.check()
        raise AssertionError("no debía llegar aquí")
    assert activity.events[0]["text"] == "Detenido: Revisión" and activity.events[0]["level"] == "warn"

    try:
        with activity.task("sync", "Sincronizando"):
            raise RuntimeError("sin conexión")
    except RuntimeError:
        pass
    assert activity.events[0]["text"] == "Error en «Sincronizando»: sin conexión"


class SlowAdvisor(MockAdvisor):
    """La IA "tarda": cada análisis espera hasta que la prueba lo libera."""

    def __init__(self):
        self.started = threading.Event()
        self.release = threading.Event()

    def analyze(self, data):
        self.started.set()
        self.release.wait(5)
        return super().analyze(data)


def make(advisor=None, background=True) -> Curator:
    curator = Curator(
        Database(":memory:"), advisor or MockAdvisor(), FakeConfluence(), "DEMO", jira=FakeJira(), background=background
    )
    curator.sync()
    return curator


def test_stop_cancels_running_and_queued_analyses():
    advisor = SlowAdvisor()
    curator = make(advisor)
    notes = [curator.create_note(f"Las perillas ahora son cinco ({i})", "Chat", "", []) for i in range(3)]
    for note_id in notes:
        curator.enqueue(note_id)
    assert advisor.started.wait(5)
    snapshot = curator.activity.snapshot()
    assert snapshot["tasks"][0]["label"] == f"Analizando la nota #{notes[0]}" and curator.queued() == notes[1:]

    message = curator.stop()
    assert message == "Detenido: 1 tarea detenida, 2 análisis en cola cancelados. Las revisiones automáticas quedan en pausa."
    assert curator.paused and curator.queued() == []
    advisor.release.set()  # la IA responde después de detener: la respuesta se descarta
    curator.wait_idle()
    for note_id in notes:
        note = curator.note(note_id)
        assert note["analyzing"] == 0 and "Análisis detenido" in note["error"]
        assert curator.proposals(note_id) == []
    assert any(e["text"] == f"Detenido: Analizando la nota #{notes[0]}" for e in curator.activity.events)

    # Lo que se pide después de detener funciona con normalidad.
    later = curator.create_note("Las perillas ahora son cinco", "Chat", "", [])
    curator.enqueue(later)
    curator.wait_idle()
    assert curator.proposals(later) and curator.note(later)["error"] == ""
    curator.resume()
    assert not curator.paused


def test_stopping_an_import_keeps_what_was_done():
    curator = make(background=False)
    original = curator.jira.issue

    def issue(key, *args):
        result = original(key, *args)
        if key == "SIM-101":
            curator.stop()  # el usuario pulsa Detener mientras se importa
        return result

    curator.jira.issue = issue
    curator.start_job("import", "Importando 3 tickets de Jira", lambda task: str(curator.import_issues(["SIM-101", "SIM-102", "SIM-103"], task)))
    rows = curator.db.all("SELECT source_ref, error FROM notes")
    assert [(r["source_ref"]) for r in rows] == ["SIM-101"]  # los demás no se trajeron
    assert "Análisis detenido" in rows[0]["error"]  # su análisis se descartó
    assert curator.activity.events[0]["text"] == "Detenido: Importando 3 tickets de Jira"


def test_stopping_the_documentation_review():
    class StoppingComparer(MockAdvisor):
        can_compare = True

        def __init__(self):
            self.calls = 0

        def compare(self, a, b):
            self.calls += 1
            curator.stop()
            return ContradictionCheck(contradicts=False, explanation="", outdated="unknown", confidence=0, suggestion="")

    advisor = StoppingComparer()
    curator = make(advisor, background=False)
    curator.auditor.save(True, 24, 0, 30)
    assert curator.auditor.run() == "Revisión detenida antes de terminar."
    assert advisor.calls == 1 and curator.auditor.settings()["last_result"] == "Revisión detenida antes de terminar."
    assert not curator.auditor.settings()["running"]


def test_publishing_and_sync_show_in_activity():
    curator = make(background=False)
    note_id = curator.create_note("Las perillas ahora son cinco", "Chat", "", [])
    curator.enqueue(note_id)
    [proposal] = curator.proposals(note_id)
    curator.apply(proposal["id"], proposal["new_storage"])
    event = curator.activity.events[0]
    page = curator.page(proposal["page_id"])
    assert event["text"].startswith(f"Publicado en «{page.title}»: ") and event["link"] == f"/demo/pages/{page.id}"

    client = TestClient(create_app(curator, Settings()))
    assert "Sincronizando el índice" in client.post("/sync").text
    assert client.get("/activity").json()["events"][0]["text"] == "Índice actualizado: 4 páginas."


def test_activity_endpoints():
    curator = make(background=False)
    client = TestClient(create_app(curator, Settings()))
    state = client.get("/activity").json()
    assert state["busy"] is False and state["paused"] is False and state["tasks"] == [] and state["queued"] == []

    stopped = client.post("/activity/stop", headers={"accept": "application/json"}).json()
    assert stopped["message"].startswith("Detenido: 0 tareas detenidas") and client.get("/activity").json()["paused"]
    page = client.get("/")
    assert "Reanudar" in page.text and 'id="activity"' in page.text

    resumed = client.post("/activity/resume", headers={"referer": "http://testserver/jira?x=1"})
    assert resumed.url.path == "/jira" and "reanudadas" in resumed.text
    assert not client.get("/activity").json()["paused"]


def test_task_started_after_stop_with_old_generation_is_cancelled():
    activity = Activity()
    generation = activity.generation
    activity.stop()
    with activity.task("analysis", "Analizando", generation=generation) as task:
        task.check()
        raise AssertionError("no debía llegar aquí")
    assert activity.events[0]["text"] == "Detenido: Analizando"


def test_layout_and_stylesheet_version():
    curator = make(background=False)
    page = TestClient(create_app(curator, Settings())).get("/").text
    assert '<div class="layout">' in page and page.index("</main>") < page.index('<aside id="activity"')
    assert '/static/style.css?v=' in page and "{{" not in page


def test_streaming_advisor_stops_mid_answer_and_queue_has_no_duplicates():
    from curator.activity import Cancelled

    class Streaming(MockAdvisor):
        """Como Ollama: recibe la tarea, informa avance y corta en cuanto se detiene."""

        def __init__(self):
            self.started = threading.Event()

        def analyze(self, data, task=None):
            task.update(detail="redactando")
            self.started.set()
            for _ in range(100):
                if task.cancelled():
                    raise Cancelled()
                threading.Event().wait(0.05)
            raise AssertionError("debía detenerse")

    advisor = Streaming()
    curator = make(advisor)
    first = curator.create_note("Las perillas ahora son cinco", "Chat", "", [])
    second = curator.create_note("Otra nota", "Chat", "", [])
    curator.enqueue(first)
    assert advisor.started.wait(5)
    curator.enqueue(second)
    curator.enqueue(second)  # «Analizar de nuevo» con la nota ya en cola: no se duplica
    assert curator.queued() == [second]
    assert curator.activity.snapshot()["tasks"][0]["detail"] == "redactando"
    curator.stop()
    curator.wait_idle()
    for note_id in (first, second):
        assert curator.note(note_id)["error"].startswith("Análisis detenido")


def test_note_page_hides_reanalyze_while_analyzing():
    curator = make(background=False)
    note_id = curator.create_note("x", "Chat", "", [])
    curator.db.execute("UPDATE notes SET analyzing = 1 WHERE id = ?", (note_id,))
    client = TestClient(create_app(curator, Settings()))
    assert "Analizar de nuevo" not in client.get(f"/notes/{note_id}").text
    curator.db.execute("UPDATE notes SET analyzing = 0 WHERE id = ?", (note_id,))
    assert "Analizar de nuevo" in client.get(f"/notes/{note_id}").text


def test_local_model_gets_fewer_candidates(monkeypatch, tmp_path):
    from curator.config import load_settings

    monkeypatch.setenv("CURATOR_ENV_FILE", str(tmp_path / "none.env"))
    monkeypatch.delenv("CURATOR_CANDIDATES", raising=False)
    monkeypatch.setenv("CURATOR_ADVISOR", "ollama")
    assert load_settings().candidates == 5
    monkeypatch.setenv("CURATOR_ADVISOR", "mock")
    assert load_settings().candidates == 10
    monkeypatch.setenv("CURATOR_CANDIDATES", "3")
    assert load_settings().candidates == 3
