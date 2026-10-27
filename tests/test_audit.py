import re
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from curator.advisor import ContradictionCheck, MockAdvisor, SectionRef
from curator.audit import parse_time
from curator.config import Settings
from curator.db import Database
from curator.demo import FakeConfluence, FakeJira
from curator.service import Curator
from curator.web import create_app


class Comparer(MockAdvisor):
    """Detecta como contradicción dos secciones que dan valores distintos en bar."""

    name = "comparer"
    can_compare = True

    def __init__(self):
        self.calls = []

    def compare(self, a: SectionRef, b: SectionRef) -> ContradictionCheck:
        self.calls.append((a.heading, b.heading))
        values = [set(re.findall(r"(\d+(?:,\d+)?) bar", ref.storage)) for ref in (a, b)]
        if all(values) and values[0] != values[1]:
            return ContradictionCheck(
                contradicts=True,
                explanation=f"«{a.heading}» dice {', '.join(values[0])} bar y «{b.heading}» dice {', '.join(values[1])} bar.",
                outdated="b",
                confidence=0.9,
                suggestion="Unificar el umbral con el manual vigente.",
            )
        return ContradictionCheck(contradicts=False, explanation="", outdated="unknown", confidence=0.1, suggestion="")


def make(advisor=None, jira=True) -> Curator:
    curator = Curator(
        Database(":memory:"), advisor or Comparer(), FakeConfluence(), "DEMO", jira=FakeJira() if jira else None, background=False
    )
    curator.sync()
    return curator


def open_findings(curator, kind=None):
    rows = curator.auditor.findings("open")
    return [r for r in rows if kind is None or r["kind"] == kind]


def test_parse_time_formats():
    assert parse_time("2026-10-18T12:00:00.000-0500") == datetime(2026, 10, 18, 17, tzinfo=timezone.utc)
    assert parse_time("2026-04-15T10:00:00.000Z") == datetime(2026, 4, 15, 10, tzinfo=timezone.utc)
    assert parse_time("") is None and parse_time("ayer") is None


def test_ticket_findings():
    curator = make()
    curator.auditor.save(True, 24, 0, 0)
    curator.auditor.run()
    [finding] = open_findings(curator, "ticket")
    assert finding["issue_key"] == "SIM-103" and finding["page_id"] == "4"
    assert "SIM-103 cambió después de la última edición de «Criterios de QA»" == finding["title"]
    assert "se actualizó el 2026-10-18" in finding["detail"] and "2026-04-15" in finding["detail"]

    note_id = curator.auditor.fix(finding["id"])
    assert curator.note(note_id)["source_ref"] == "SIM-103"
    assert curator.auditor.finding(finding["id"])["note_id"] == note_id


def test_fetch_issues_isolates_unknown_keys():
    curator = make()
    issues = curator.auditor._fetch_issues(["ISO-9001", "SIM-101", "ABC-1", "SIM-103"])
    assert sorted(issues) == ["SIM-101", "SIM-103"]


def test_stale_findings_and_review():
    curator = make()
    curator.auditor.save(True, 24, 180, 0)
    curator.auditor.run()
    stale = open_findings(curator, "stale")
    assert len(stale) == 4 and "no cambia hace" in stale[0]["title"]

    curator.auditor.dismiss(stale[0]["id"])
    curator.auditor.run()
    assert len(open_findings(curator, "stale")) == 3  # el descartado no vuelve

    page = curator.confluence.pages["4"]
    curator.confluence.update_page(page, page.body + "<p>Revisado.</p>", "revisión")
    curator.auditor.run()
    titles = [f["page_id"] for f in open_findings(curator, "stale")]
    assert "4" not in titles


def test_contradictions_are_cached_fixed_and_resolved():
    advisor = Comparer()
    curator = make(advisor, jira=False)
    curator.auditor.save(True, 24, 0, 30)
    result = curator.auditor.run()
    [finding] = open_findings(curator, "contradiction")
    assert finding["page_id"] == "3" and finding["other_page_id"] == "2"  # la simulación parece desactualizada
    assert "Alarmas en la simulación" in finding["title"] and "7 bar" in finding["detail"]
    assert finding["confidence"] == 0.9 and "comparaciones nuevas" in result
    calls = len(advisor.calls)
    assert calls > 0

    curator.auditor.run()
    assert len(advisor.calls) == calls  # nada cambió: no se vuelve a comparar
    assert len(open_findings(curator, "contradiction")) == 1

    note_id = curator.auditor.fix(finding["id"])
    note = curator.note(note_id)
    assert note["source_type"] == "Revisión" and note["source_ref"] == f"Hallazgo #{finding['id']}"
    assert "Lo que dice «Producto real: manual de referencia › Alarmas»" in note["text"]
    [proposal] = curator.proposals(note_id)
    assert (proposal["page_id"], proposal["section_index"]) == ("3", 1)

    page = curator.confluence.pages["3"]
    curator.confluence.update_page(page, page.body.replace("7 bar", "8 bar"), "corrección")
    curator.auditor.run()
    assert open_findings(curator, "contradiction") == []
    assert curator.auditor.finding(finding["id"])["status"] == "resolved"


def test_unrelated_edit_resolves_then_reopens():
    curator = make(Comparer(), jira=False)
    curator.auditor.save(True, 24, 0, 30)
    curator.auditor.run()
    [finding] = open_findings(curator, "contradiction")
    page = curator.confluence.pages["3"]
    edited = page.body.replace("con la muñeca.</p>", "con la muñeca. Otro tema.</p>")  # otra sección de la página
    curator.confluence.update_page(page, edited, "otro cambio")
    curator.auditor.run()
    [again] = open_findings(curator, "contradiction")
    assert again["id"] == finding["id"] and again["page_version"] == 2


def test_budget_limits_new_comparisons():
    advisor = Comparer()
    curator = make(advisor, jira=False)
    curator.auditor.save(True, 24, 0, 1)
    curator.auditor.run()
    assert len(advisor.calls) == 1
    curator.auditor.run()
    assert len(advisor.calls) == 2  # la siguiente revisión sigue con las pendientes


def test_without_ai_only_heuristics():
    curator = make(MockAdvisor())
    curator.auditor.save(True, 24, 180, 30)
    result = curator.auditor.run()
    assert "las contradicciones necesitan IA" in result
    assert open_findings(curator, "contradiction") == []
    assert open_findings(curator, "ticket")


def test_due_and_validation():
    curator = make()
    auditor = curator.auditor
    assert not auditor.due()
    auditor.save(True, 6, 180, 30)
    assert auditor.due()
    auditor.run()
    assert not auditor.due()
    assert auditor.due(datetime.now(timezone.utc) + timedelta(hours=7))
    for args in [(True, 0, 180, 30), (True, 6, -1, 30), (True, 6, 180, 501)]:
        try:
            auditor.save(*args)
            raise AssertionError("debía fallar")
        except ValueError:
            pass


def test_errors_are_recorded():
    curator = make()

    def broken():
        raise RuntimeError("Confluence no responde")

    curator.sync = broken
    try:
        curator.auditor.run()
        raise AssertionError("debía fallar")
    except RuntimeError:
        pass
    assert curator.auditor.settings()["last_error"] == "Confluence no responde"
    assert not curator.auditor.settings()["running"]


def test_audit_web_flow():
    curator = make()
    client = TestClient(create_app(curator, Settings()))
    assert "Todavía no se hizo ninguna revisión" in client.get("/audit").text
    saved = client.post("/audit/settings", data={"enabled": "1", "interval": "12", "stale_days": "0", "max_pairs": "30"})
    assert "Configuración guardada" in saved.text
    curator.auditor.run()
    page = client.get("/audit")
    assert "Contradicción" in page.text and "Importar SIM-103" in page.text and "Proponer corrección" in page.text
    assert "hallazgos abiertos" in client.get("/").text

    finding = open_findings(curator, "contradiction")[0]
    fixed = client.post(f"/audit/findings/{finding['id']}/fix")
    assert fixed.url.path.startswith("/notes/") and "Aprobar y publicar" in fixed.text
    ticket = open_findings(curator, "ticket")[0]
    assert "Hallazgo descartado" in client.post(f"/audit/findings/{ticket['id']}/dismiss").text
    assert "SIM-103" in client.get("/audit", params={"status": "dismissed"}).text
    assert "El intervalo mínimo" in client.post("/audit/settings", data={"interval": "0"}).text


def test_anthropic_compare_request():
    from curator.advisor import AnthropicAdvisor

    class FakeMessages:
        def parse(self, **kwargs):
            self.kwargs = kwargs

            class Response:
                stop_reason = "end_turn"
                parsed_output = ContradictionCheck(contradicts=False, explanation="", outdated="unknown", confidence=0, suggestion="")

            return Response()

    class FakeClient:
        def __init__(self):
            self.beta = type("Beta", (), {"messages": FakeMessages()})()

    fake = FakeClient()
    AnthropicAdvisor("claude-opus-5-5", "high", client=fake).compare(
        SectionRef("Manual", "Alarmas", "<p>8 bar</p>", "2026-04-15"), SectionRef("Simulación", "Alarmas", "<p>7 bar</p>", "")
    )
    kwargs = fake.beta.messages.kwargs
    assert kwargs["output_format"] is ContradictionCheck and kwargs["fallbacks"] == "default"
    text = kwargs["messages"][0]["content"][0]["text"]
    assert 'last_edited="2026-04-15"' in text and 'last_edited="desconocida"' in text
