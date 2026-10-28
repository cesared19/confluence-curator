import json
import sqlite3

import httpx
from fastapi.testclient import TestClient

from curator.advisor import MockAdvisor
from curator.config import Settings
from curator.db import Database
from curator.demo import FakeConfluence, FakeJira
from curator.jira import JiraClient, adf_to_text, to_jql
from curator.service import Curator
from curator.web import create_app


def doc(*content):
    return {"type": "doc", "version": 1, "content": list(content)}


def paragraph(*content):
    return {"type": "paragraph", "content": list(content)}


def text(value, href=None):
    node = {"type": "text", "text": value}
    if href:
        node["marks"] = [{"type": "link", "attrs": {"href": href}}]
    return node


def test_to_jql_accepts_keys_or_jql():
    assert to_jql("sim-1, SIM-22  ABC_D-3") == "key in (SIM-1, SIM-22, ABC_D-3) ORDER BY key"
    assert to_jql(" project = SIM ") == "project = SIM"


def test_adf_to_text():
    adf = doc(
        {"type": "heading", "attrs": {"level": 2}, "content": [text("Contexto")]},
        paragraph(text("Ver "), text("manual", "https://x.com/m.pdf"), {"type": "hardBreak"}, {"type": "mention", "attrs": {"text": "@Ana"}}),
        {"type": "bulletList", "content": [
            {"type": "listItem", "content": [paragraph(text("uno"))]},
            {"type": "listItem", "content": [paragraph(text("dos"))]},
        ]},
        {"type": "table", "content": [{"type": "tableRow", "content": [
            {"type": "tableHeader", "content": [paragraph(text("Campo"))]},
            {"type": "tableCell", "content": [paragraph(text("Valor"))]},
        ]}]},
        {"type": "mediaSingle", "content": [{"type": "media", "attrs": {"id": "1"}}]},
        {"type": "codeBlock", "content": [text("x = 1")]},
    )
    result = adf_to_text(adf)
    assert "Contexto\n\nVer manual (https://x.com/m.pdf)\n@Ana" in result
    assert "- uno\n- dos" in result
    assert "Campo | Valor" in result
    assert "[imagen o archivo adjunto]" in result
    assert "```\nx = 1\n```" in result
    assert adf_to_text(None) == ""


def make_client(handler) -> JiraClient:
    return JiraClient("https://acme.atlassian.net/", "yo@acme.com", "token", transport=httpx.MockTransport(handler))


def test_search_paginates_with_tokens():
    bodies = []

    def handler(request):
        body = json.loads(request.content)
        bodies.append(body)
        issue = lambda k: {"key": k, "fields": {"summary": f"s {k}", "status": {"name": "Hecho"}, "issuetype": {"name": "Bug"}, "updated": "2026-10-20T10:00:00.000-0500"}}
        if "nextPageToken" not in body:
            return httpx.Response(200, json={"issues": [issue("SIM-1")], "nextPageToken": "t2", "isLast": False})
        return httpx.Response(200, json={"issues": [issue("SIM-2")], "isLast": True})

    issues = make_client(handler).search("SIM-1, SIM-2")
    assert [(i.key, i.status, i.issue_type) for i in issues] == [("SIM-1", "Hecho", "Bug"), ("SIM-2", "Hecho", "Bug")]
    assert bodies[0]["jql"] == "key in (SIM-1, SIM-2) ORDER BY key"
    assert bodies[1]["nextPageToken"] == "t2"


def test_issue_builds_text_comments_and_images():
    requested = []

    def handler(request):
        requested.append(request.url.path)
        path = request.url.path
        if path == "/rest/api/3/issue/SIM-7":
            return httpx.Response(200, json={"key": "SIM-7", "fields": {
                "summary": "Umbral de alarma",
                "issuetype": {"name": "Bug"},
                "status": {"name": "En curso"},
                "labels": ["alarmas"],
                "reporter": {"displayName": "Ana"},
                "updated": "2026-10-20T10:00:00.000-0500",
                "description": doc(paragraph(text("Ahora 7,5 bar."))),
                "attachment": [
                    {"filename": "panel.png", "mimeType": "image/png", "size": 10, "content": "https://acme.atlassian.net/rest/api/3/attachment/content/1"},
                    {"filename": "log.txt", "mimeType": "text/plain", "size": 10, "content": "https://acme.atlassian.net/rest/api/3/attachment/content/2"},
                    {"filename": "enorme.png", "mimeType": "image/png", "size": 999999999, "content": "https://acme.atlassian.net/rest/api/3/attachment/content/3"},
                ],
            }})
        if path == "/rest/api/3/issue/SIM-7/comment":
            return httpx.Response(200, json={"total": 1, "comments": [
                {"author": {"displayName": "Luis"}, "created": "2026-10-21T09:00:00.000-0500", "body": doc(paragraph(text("Confirmado.")))}
            ]})
        if path == "/rest/api/3/attachment/content/1":
            return httpx.Response(200, content=b"PNGDATA")
        return httpx.Response(404, json={"errorMessages": ["no existe"]})

    issue = make_client(handler).issue("SIM-7", max_images=5, max_image_bytes=1000)
    assert issue.url == "https://acme.atlassian.net/browse/SIM-7"
    assert issue.text.startswith("SIM-7: Umbral de alarma\nTipo: Bug\nEstado: En curso\nEtiquetas: alarmas\nReportado por: Ana")
    assert "Descripción:\nAhora 7,5 bar." in issue.text
    assert "- Luis (2026-10-21): Confirmado." in issue.text
    assert "Se omitieron 1 imágenes" in issue.text
    assert issue.images == [("panel.png", "image/png", b"PNGDATA")]
    assert "/rest/api/3/attachment/content/2" not in requested


def test_jira_error_message():
    client = make_client(lambda r: httpx.Response(400, json={"errorMessages": ["Error en la consulta JQL"]}))
    try:
        client.search("project = ")
        raise AssertionError("debía fallar")
    except Exception as error:
        assert "400" in str(error) and "Error en la consulta JQL" in str(error)


def make_curator(background=False) -> Curator:
    curator = Curator(Database(":memory:"), MockAdvisor(), FakeConfluence(), "DEMO", jira=FakeJira(), background=background)
    curator.sync()
    return curator


def test_import_creates_notes_and_tracks_changes():
    curator = make_curator()
    results = curator.jira_search("SIM-101, SIM-102")
    assert [(r["issue"].key, r["last"], r["changed"]) for r in results] == [("SIM-101", None, False), ("SIM-102", None, False)]

    note_ids, errors = curator.import_issues(["SIM-101", "SIM-101", "SIM-999"])
    assert len(note_ids) == 1 and errors and "SIM-999" in errors[0]
    note = curator.note(note_ids[0])
    assert note["source_type"] == "Jira" and note["source_ref"] == "SIM-101"
    assert "Enlace: https://demo.atlassian.net/browse/SIM-101" in note["text"]
    assert note["analyzing"] == 0
    assert curator.proposals(note_ids[0])[0]["page_id"] == "2"

    [result] = curator.jira_search("SIM-101")
    assert result["last"]["note_id"] == note_ids[0] and not result["changed"]
    curator.db.execute("UPDATE jira_imports SET issue_updated = '2026-10-20T09:00:00.000-0500'")
    [result] = curator.jira_search("SIM-101")
    assert result["changed"]
    # Mismo instante en otra zona horaria: no cuenta como cambio.
    curator.db.execute("UPDATE jira_imports SET issue_updated = '2026-10-20T15:00:00.000+0000'")
    assert not curator.jira_search("SIM-101")[0]["changed"]
    assert curator.db.get_meta("last_jql") == "SIM-101"


def test_background_queue_analyzes_in_order():
    curator = make_curator(background=True)
    note_ids, _ = curator.import_issues(["SIM-101", "SIM-102", "SIM-103"])
    curator.wait_idle()
    for note_id in note_ids:
        assert curator.note(note_id)["analyzing"] == 0
        assert curator.proposals(note_id)


def test_resume_pending_after_restart():
    curator = make_curator()
    note_id = curator.create_note("Las perillas ahora son cinco", "Chat", "", [])
    curator.db.execute("UPDATE notes SET analyzing = 1 WHERE id = ?", (note_id,))
    curator.resume_pending()
    assert curator.note(note_id)["analyzing"] == 0 and curator.proposals(note_id)


def test_worker_records_unexpected_errors():
    curator = make_curator()
    note_id = curator.create_note("x", "Chat", "", [])
    curator.enqueue(note_id, target_page="no-existe")
    note = curator.note(note_id)
    assert note["analyzing"] == 0 and "no está en el índice" in note["error"]


def test_old_database_gets_new_column(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE notes (id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL, text TEXT NOT NULL, "
        "source_type TEXT NOT NULL, source_ref TEXT NOT NULL, summary TEXT NOT NULL DEFAULT '', "
        "no_change_reason TEXT NOT NULL DEFAULT '', alternatives TEXT NOT NULL DEFAULT '[]', error TEXT NOT NULL DEFAULT '')"
    )
    conn.execute("INSERT INTO notes(created_at, text, source_type, source_ref) VALUES ('x', 'nota vieja', 'Chat', '')")
    conn.commit()
    conn.close()
    db = Database(str(path))
    assert db.one("SELECT analyzing FROM notes")["analyzing"] == 0


def test_jira_web_flow():
    curator = make_curator()
    client = TestClient(create_app(curator, Settings()))
    page = client.get("/jira", params={"q": "SIM-101, SIM-103"})
    assert 'value="SIM-101"' in page.text and 'value="SIM-102"' not in page.text and "Nuevo" in page.text

    one = client.post("/jira/import", data={"keys": ["SIM-101"]})
    assert one.url.path.startswith("/notes/") and "Aprobar y publicar" in one.text

    many = client.post("/jira/import", data={"keys": ["SIM-102", "SIM-103"]})
    assert many.url.path == "/" and "2 tickets importados" in many.text

    assert "Selecciona al menos" in client.post("/jira/import", data={}).text
    assert "Importado" in client.get("/jira", params={"q": "SIM-101"}).text


def test_jira_not_configured():
    curator = Curator(Database(":memory:"), MockAdvisor(), FakeConfluence(), "DEMO", background=False)
    client = TestClient(create_app(curator, Settings()))
    page = client.get("/jira", params={"q": "SIM-1"})
    assert "Jira no está configurado" in page.text


def test_watch_query_strips_order_and_filters_by_day():
    from curator.service import watch_query

    assert watch_query("project = SIM order by created DESC", "2026-10-27T03:00:00+00:00") == (
        '(project = SIM) AND updated >= "2026-10-26" ORDER BY updated DESC'
    )
    assert watch_query("SIM-1, SIM-2", "2026-10-27T03:00:00+00:00").startswith("(key in (SIM-1, SIM-2)) AND")


def test_watch_imports_only_new_changes():
    from datetime import datetime, timedelta, timezone

    curator = make_curator()
    jira = curator.jira
    curator.save_watch("project = SIM", 30, True)
    assert curator.watch_settings()["enabled"] and curator.watch_settings()["since"]
    assert jira.queries[-1].startswith("(project = SIM) AND updated >=")

    # Los tickets de ejemplo son anteriores a la activación: no se importan solos.
    assert curator.run_watch() == "sin tickets nuevos ni cambios."
    assert curator.db.one("SELECT COUNT(*) AS n FROM notes")["n"] == 0

    later = (datetime.now(timezone.utc) + timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%S.000+0000")
    jira.touch("SIM-102", later, ("Ana Pérez", "El volumen va de 0 a 10."))
    assert curator.run_watch() == "1 tickets importados."
    note = curator.recent_notes()[0]
    assert note["source_ref"] == "SIM-102" and "El volumen va de 0 a 10." in note["text"]

    assert curator.run_watch() == "sin tickets nuevos ni cambios."
    even_later = (datetime.now(timezone.utc) + timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%S.000+0000")
    jira.touch("SIM-102", even_later)
    assert curator.run_watch() == "1 tickets importados."
    settings = curator.watch_settings()
    assert settings["last_run"] and settings["last_error"] == ""


def test_watch_batch_limit(monkeypatch):
    from datetime import datetime, timedelta, timezone

    curator = make_curator()
    curator.save_watch("project = SIM", 30, True)
    later = (datetime.now(timezone.utc) + timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%S.000+0000")
    for key in ("SIM-101", "SIM-102", "SIM-103"):
        curator.jira.touch(key, later)
    monkeypatch.setattr(Curator, "WATCH_BATCH", 2)
    assert curator.run_watch() == "2 tickets importados; 1 quedan para la próxima revisión."
    assert curator.run_watch() == "1 tickets importados."


def test_watch_due_and_validation():
    from datetime import datetime, timedelta, timezone

    curator = make_curator()
    assert not curator.watch_due()
    for jql, interval in [("", 30), ("project = SIM", 1)]:
        try:
            curator.save_watch(jql, interval, True)
            raise AssertionError("debía fallar")
        except Exception as error:
            assert "JQL" in str(error) or "5 minutos" in str(error)
    curator.save_watch("project = SIM", 30, True)
    assert curator.watch_due()
    curator.run_watch()
    assert not curator.watch_due()
    assert curator.watch_due(datetime.now(timezone.utc) + timedelta(minutes=31))
    curator.save_watch("project = SIM", 30, False)
    assert not curator.watch_due(datetime.now(timezone.utc) + timedelta(days=1))


def test_watch_records_errors():
    curator = make_curator()
    curator.save_watch("project = SIM", 30, True)

    def broken(query, limit):
        from curator.jira import JiraError

        raise JiraError("Jira respondió 401: credenciales inválidas", 401)

    curator.jira.search = broken
    try:
        curator.run_watch()
        raise AssertionError("debía fallar")
    except Exception:
        pass
    assert "401" in curator.watch_settings()["last_error"]


def test_watch_web_flow():
    curator = make_curator()
    client = TestClient(create_app(curator, Settings()))
    saved = client.post("/jira/watch", data={"jql": "project = SIM", "interval": "15", "enabled": "1"})
    assert "Revisión automática activada" in saved.text and "Revisar ahora" in saved.text
    ran = client.post("/jira/watch/run")
    assert "Revisión de Jira: sin tickets nuevos ni cambios." in ran.text
    assert "Revisión automática de Jira activa (cada 15 min)" in client.get("/").text
    bad = client.post("/jira/watch", data={"jql": "", "interval": "15", "enabled": "1"})
    assert "Escribe la consulta JQL" in bad.text


def test_jira_page_title_is_plain_text():
    curator = make_curator()
    page = TestClient(create_app(curator, Settings())).get("/jira").text
    title = page[page.index("<title>") : page.index("</title>")]
    assert title == "<title>Jira · Confluence Curator"
