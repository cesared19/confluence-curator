import json

import httpx
from fastapi.testclient import TestClient

from curator.advisor import AdvisorInput, AnthropicAdvisor, Analysis, Candidate
from curator.config import Settings
from curator.confluence import ConfluenceClient, Page
from curator.db import Database
from curator.demo import FakeConfluence
from curator.advisor import MockAdvisor
from curator.service import Curator
from curator.web import create_app


def test_confluence_client_paginates_and_updates():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        path = request.url.path
        if path == "/wiki/api/v2/spaces":
            return httpx.Response(200, json={"results": [{"id": 77, "key": "SIM"}]})
        if path == "/wiki/api/v2/spaces/77/pages" and "cursor" not in request.url.params:
            return httpx.Response(200, json={
                "results": [{"id": "1", "title": "Inicio", "version": {"number": 3}, "body": {"storage": {"value": "<p>a</p>"}}}],
                "_links": {"next": "/wiki/api/v2/spaces/77/pages?cursor=abc"},
            })
        if path == "/wiki/api/v2/spaces/77/pages":
            return httpx.Response(200, json={"results": [{"id": "2", "title": "Hija", "parentId": "1", "version": {"number": 1}}], "_links": {}})
        if path == "/wiki/api/v2/pages/1" and request.method == "PUT":
            return httpx.Response(200, json={})
        if path == "/wiki/api/v2/pages/1":
            return httpx.Response(200, json={"id": "1", "title": "Inicio", "version": {"number": 4}, "body": {"storage": {"value": "<p>b</p>"}}})
        if path == "/wiki/rest/api/content/1/child/attachment":
            return httpx.Response(200, json={})
        return httpx.Response(404, json={"message": "no"})

    client = ConfluenceClient("https://acme.atlassian.net", "yo@acme.com", "token", transport=httpx.MockTransport(handler))
    assert client.space_id("SIM") == "77"
    pages = client.list_pages("77")
    assert [(p.id, p.parent_id, p.version) for p in pages] == [("1", None, 3), ("2", "1", 1)]

    updated = client.update_page(Page("1", "Inicio", None, 3, "<p>a</p>"), "<p>b</p>", "cambio")
    put = next(c for c in calls if c.method == "PUT" and c.url.path == "/wiki/api/v2/pages/1")
    payload = json.loads(put.content)
    assert payload["version"] == {"number": 4, "message": "cambio"}
    assert payload["body"] == {"representation": "storage", "value": "<p>b</p>"}
    assert put.headers["authorization"].startswith("Basic ")
    assert updated.version == 4

    client.upload_attachment("1", "a.png", b"x", "image/png", "c")
    upload = calls[-1]
    assert upload.headers["x-atlassian-token"] == "nocheck"
    assert client.page_url("1") == "https://acme.atlassian.net/wiki/pages/viewpage.action?pageId=1"


def test_confluence_error_message():
    client = ConfluenceClient(
        "https://acme.atlassian.net", "a", "b",
        transport=httpx.MockTransport(lambda r: httpx.Response(409, json={"message": "Version conflict"})),
    )
    try:
        client.get_page("1")
    except Exception as error:
        assert "409" in str(error) and "Version conflict" in str(error)


def test_anthropic_advisor_request_shape():
    class FakeMessages:
        def parse(self, **kwargs):
            self.kwargs = kwargs

            class Response:
                stop_reason = "end_turn"
                parsed_output = Analysis(note_summary="s", changes=[], alternatives=[], no_change_reason="ya está")

            return Response()

    class FakeClient:
        def __init__(self):
            self.beta = type("Beta", (), {"messages": FakeMessages()})()

    fake = FakeClient()
    advisor = AnthropicAdvisor("claude-opus-5-5", "high", client=fake)
    result = advisor.analyze(AdvisorInput(
        note_text="nota", source="Jira X-1", images=[("image/png", b"abc")],
        candidates=[Candidate("2", "Manual", 0, "Alarmas", "<h2>Alarmas</h2>", 1.0)], page_tree="- [2] Manual",
    ))
    kwargs = fake.beta.messages.kwargs
    assert result.no_change_reason == "ya está"
    assert kwargs["model"] == "claude-opus-5-5"
    assert kwargs["output_format"] is Analysis
    assert kwargs["output_config"] == {"effort": "high"}
    assert kwargs["fallbacks"] == "default"
    content = kwargs["messages"][0]["content"]
    assert content[0]["type"] == "image" and content[0]["source"]["data"] == "YWJj"
    assert 'section_index="0"' in content[1]["text"] and "<note source=\"Jira X-1\"" in content[1]["text"]


def test_web_flow():
    curator = Curator(Database(":memory:"), MockAdvisor(), FakeConfluence(), "DEMO", background=False)
    client = TestClient(create_app(curator, Settings()))
    assert "índice está vacío" in client.get("/").text
    client.post("/sync")
    response = client.post(
        "/notes",
        data={"text": "Las perillas ahora son cinco", "source_type": "Manual del producto", "source_ref": ""},
    )
    assert response.url.path == "/notes/1"
    assert "Aprobar y publicar" in response.text
    proposal = curator.proposals(1)[0]
    response = client.post(f"/proposals/{proposal['id']}/apply", data={"content": proposal["new_storage"]})
    assert "Cambio publicado" in response.text
    assert "Publicada" in client.get("/history").text
    assert "Panel de control" in client.get("/search", params={"q": "perillas"}).text

    bad = client.post("/notes", data={"text": "x"}, files=[("images", ("a.txt", b"hola", "text/plain"))])
    assert "solo se aceptan" in bad.text


def test_basic_auth():
    curator = Curator(Database(":memory:"), MockAdvisor(), None, "")
    client = TestClient(create_app(curator, Settings(app_user="yo", app_password="secreto")))
    assert client.get("/").status_code == 401
    assert client.get("/", auth=("yo", "secreto")).status_code == 200


def test_ollama_advisor_request_and_parsing():
    from curator.advisor import OllamaAdvisor, inline_refs

    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["payload"] = json.loads(request.content)
        answer = Analysis(note_summary="s", changes=[], alternatives=[], no_change_reason="ya documentado")
        return httpx.Response(200, json={"message": {"content": answer.model_dump_json()}, "done_reason": "stop"})

    advisor = OllamaAdvisor("http://localhost:11434/", "gemma3:12b", 8192, transport=httpx.MockTransport(handler))
    result = advisor.analyze(AdvisorInput(note_text="nota", source="Chat", images=[("image/png", b"abc")]))
    payload = seen["payload"]
    assert seen["path"] == "/api/chat" and result.no_change_reason == "ya documentado"
    assert payload["model"] == "gemma3:12b" and payload["stream"] is False
    assert payload["options"] == {"temperature": 0, "num_ctx": 8192}
    assert payload["messages"][1]["images"] == ["YWJj"]
    assert "$ref" not in json.dumps(payload["format"]) and "$defs" not in payload["format"]
    assert payload["format"] == inline_refs(Analysis.model_json_schema())


def test_ollama_advisor_errors():
    from curator.advisor import OllamaAdvisor

    def bad_json(request):
        return httpx.Response(200, json={"message": {"content": "no es json"}, "done_reason": "stop"})

    advisor = OllamaAdvisor("http://x", "m", 1024, transport=httpx.MockTransport(bad_json))
    try:
        advisor.analyze(AdvisorInput(note_text="n", source=""))
        raise AssertionError("debía fallar")
    except RuntimeError as error:
        assert "no devolvió un análisis válido" in str(error)

    def cut(request):
        return httpx.Response(200, json={"message": {"content": "{"}, "done_reason": "length"})

    advisor = OllamaAdvisor("http://x", "m", 1024, transport=httpx.MockTransport(cut))
    try:
        advisor.analyze(AdvisorInput(note_text="n", source=""))
        raise AssertionError("debía fallar")
    except RuntimeError as error:
        assert "OLLAMA_CONTEXT" in str(error)
