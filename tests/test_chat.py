from datetime import datetime, timedelta, timezone

import httpx
from fastapi.testclient import TestClient

from curator.advisor import MockAdvisor
from curator.chat import Channel, ChatError, Message, SlackSource, conversation_text, group
from curator.config import Settings
from curator.db import Database
from curator.demo import FakeChat, FakeConfluence
from curator.service import Curator
from curator.web import create_app


def ts(minutes_ago: float) -> str:
    return f"{(datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).timestamp():.6f}"


def slack(handler, sleeps=None) -> SlackSource:
    return SlackSource("xoxb-test", transport=httpx.MockTransport(handler), sleep=(sleeps.append if sleeps is not None else lambda s: None))


def ok(**data):
    return httpx.Response(200, json={"ok": True, **data})


def test_slack_text_formatting():
    calls = []

    def handler(request):
        calls.append(request.url.params.get("user"))
        return ok(user={"profile": {"display_name": "Ana"}, "real_name": "Ana Pérez"})

    source = slack(handler)
    text = source._text("<@U1> mira <#C2|simulador-vr> y <https://x.com/m.pdf|el manual> &amp; <https://y.com> <!here> <@U1>")
    assert text == "@Ana mira #simulador-vr y el manual (https://x.com/m.pdf) & https://y.com @here @Ana"
    assert calls == ["U1"]  # el nombre se busca una sola vez


def test_grouping_threads_and_gaps():
    channel = Channel("C1", "general")
    raw = [
        {"ts": ts(200), "text": "a"},
        {"ts": ts(190), "text": "b"},  # 10 minutos después: misma conversación
        {"ts": ts(100), "text": "c"},  # 90 minutos después: otra
        {"ts": ts(95), "text": "hilo", "reply_count": 3, "latest_reply": ts(10)},
        {"ts": ts(90), "text": "d"},
    ]
    conversations = group(channel, raw, lambda r: Message("Ana", r["ts"], r["text"]), lambda t: f"link/{t}")
    shapes = [(c.thread, [m.text for m in c.messages], c.size) for c in conversations]
    assert shapes == [(False, ["d"], 1), (True, ["hilo"], 4), (False, ["c"], 1), (False, ["a", "b"], 2)]
    assert conversations[1].last_ts == raw[3]["latest_reply"] and conversations[3].key == f"C1:{raw[0]['ts']}"


def test_slack_source_end_to_end():
    parent, reply, loose = ts(60), ts(50), ts(30)
    requests = []

    def handler(request):
        method = request.url.path.rsplit("/", 1)[-1]
        requests.append((method, dict(request.url.params)))
        assert request.headers["authorization"] == "Bearer xoxb-test"
        if method == "auth.test":
            return ok(team="Simulador VR", user="curator", url="https://simvr.slack.com/")
        if method == "conversations.list":
            if not request.url.params.get("cursor"):
                return ok(channels=[{"id": "C1", "name": "general", "is_member": True}], response_metadata={"next_cursor": "p2"})
            return ok(channels=[{"id": "C2", "name": "privado", "is_private": True}, {"id": "C3", "name": "ajeno", "is_member": False}])
        if method == "conversations.history":
            return ok(messages=[
                {"ts": loose, "user": "U1", "text": "Llegaron fotos", "files": [
                    {"name": "panel.png", "mimetype": "image/png", "size": 10, "url_private_download": "https://files.slack.com/panel.png"},
                ]},
                {"ts": parent, "user": "U2", "text": "¿Botón B?", "reply_count": 1, "latest_reply": reply},
                {"ts": reply, "thread_ts": parent, "subtype": "thread_broadcast", "user": "U1", "text": "Sí"},
                {"ts": ts(40), "subtype": "channel_join", "user": "U3", "text": "se unió"},
            ], has_more=False)
        if method == "conversations.replies":
            return ok(messages=[
                {"ts": parent, "user": "U2", "text": "¿Botón B?"},
                {"ts": reply, "user": "U1", "text": "Sí, el B"},
            ])
        if method == "users.info":
            names = {"U1": "Ana", "U2": "Luis"}
            return ok(user={"profile": {"display_name": names[request.url.params["user"]]}})
        if request.url.host == "files.slack.com":
            return httpx.Response(200, content=b"PNG", headers={"content-type": "image/png"})
        raise AssertionError(request.url)

    source = slack(handler)
    assert source.check() == "Simulador VR como curator"
    channels = source.channels()
    assert [(c.id, c.private) for c in channels] == [("C1", False), ("C2", True)]

    conversations = source.conversations(channels[0], 7)
    assert [(c.thread, c.size) for c in conversations] == [(False, 1), (True, 2)]
    history = next(params for method, params in requests if method == "conversations.history")
    assert float(history["oldest"]) < float(parent)

    thread = source.complete(conversations[1])
    assert [(m.author, m.text) for m in thread.messages] == [("Luis", "¿Botón B?"), ("Ana", "Sí, el B")]
    assert thread.link == f"https://simvr.slack.com/archives/C1/p{parent.replace('.', '')}"
    assert source.download(conversations[0].messages[0].files[0]) == b"PNG"
    assert sum(1 for method, _ in requests if method == "users.info") == 2


def test_slack_errors_and_rate_limits():
    sleeps = []
    attempts = []

    def limited(request):
        attempts.append(1)
        if len(attempts) < 3:
            return httpx.Response(429, headers={"Retry-After": "2"})
        return ok(team="T", user="u")

    assert slack(limited, sleeps).check() == "T como u" and sleeps == [2, 2]

    for error, expected in [
        ({"error": "invalid_auth"}, "rechazó el token"),
        ({"error": "missing_scope", "needed": "channels:history"}, "falta el permiso channels:history"),
        ({"error": "not_in_channel"}, "/invite"),
    ]:
        source = slack(lambda r, e=error: httpx.Response(200, json={"ok": False, **e}))
        try:
            source.check()
            raise AssertionError("debía fallar")
        except ChatError as failure:
            assert expected in str(failure)

    login_page = slack(lambda r: httpx.Response(200, text="<html>login</html>", headers={"content-type": "text/html"}))
    try:
        login_page.download({"name": "a.png", "url": "https://files.slack.com/a.png"})
        raise AssertionError("debía fallar")
    except ChatError as failure:
        assert "files:read" in str(failure)


def test_conversation_text():
    channel = Channel("C1", "simulador-vr")
    first = Message("Ana", f"{datetime(2026, 10, 28, 15, 10, tzinfo=timezone.utc).timestamp():.6f}", "¿Botón B?\nO el A")
    second = Message("Luis", f"{datetime(2026, 10, 28, 15, 22, tzinfo=timezone.utc).timestamp():.6f}", "El B", [{"name": "foto.png"}])
    conversation = group(channel, [{"ts": first.ts, "m": first, "reply_count": 1, "latest_reply": second.ts}], lambda r: r["m"], lambda t: "https://x/p1")[0]
    conversation.messages.append(second)
    assert conversation_text(conversation, "Slack") == (
        "Hilo en Slack #simulador-vr, 2026-10-28 (horas en UTC)\n\n"
        "- Ana (15:10): ¿Botón B?\n  O el A\n"
        "- Luis (15:22): El B [adjuntó: foto.png]\n\n"
        "Enlace: https://x/p1"
    )


def make() -> Curator:
    curator = Curator(Database(":memory:"), MockAdvisor(), FakeConfluence(), "DEMO", background=False, chat=FakeChat())
    curator.sync()
    return curator


def test_import_conversations_and_track_changes():
    curator = make()
    results = curator.chat_conversations("C1", 7)
    assert [(r["conversation"].thread, r["conversation"].size, r["last"]) for r in results] == [
        (True, 3, None), (False, 2, None), (False, 1, None)
    ]
    thread, photos = results[0]["conversation"], results[1]["conversation"]

    note_ids, errors = curator.import_chat([thread.key, photos.key], "C1", 7)
    assert len(note_ids) == 2 and not errors
    thread_note = curator.note(note_ids[0])
    assert thread_note["source_type"] == "Chat" and thread_note["source_ref"].startswith("Slack #simulador-vr ")
    assert "botón B del control derecho" in thread_note["text"] and "perilla de modo" in thread_note["text"]
    [image] = curator.note_images(note_ids[1])
    assert image["filename"] == "panel.png" and image["data"].startswith(b"\x89PNG")
    assert curator.proposals(note_ids[1])  # se analizó

    again = curator.chat_conversations("C1", 7)
    assert again[0]["last"]["note_id"] == note_ids[0] and not again[0]["changed"]

    # Alguien responde en el hilo después de importarlo.
    later = f"{datetime.now(timezone.utc).timestamp():.6f}"
    curator.chat.threads[thread.messages[0].ts].append(Message("Marta", later, "Agrego: también vibra el control."))
    assert curator.chat_conversations("C1", 7)[0]["changed"]


def test_chat_validation():
    curator = make()
    for args, expected in [(("C1", 0), "entre 1 y 90"), (("C9", 7), "No se encontró el canal")]:
        try:
            curator.chat_conversations(*args)
            raise AssertionError("debía fallar")
        except Exception as error:
            assert expected in str(error)
    curator.chat = None
    try:
        curator.chat_channels()
        raise AssertionError("debía fallar")
    except Exception as error:
        assert "Ajustes" in str(error)


def test_chat_web_flow():
    curator = make()
    client = TestClient(create_app(curator, Settings()))
    page = client.get("/chat")
    assert "#simulador-vr" in page.text and "Ver conversaciones" in page.text
    listing = client.get("/chat", params={"channel": "C1", "days": 7})
    assert "3 conversaciones" in listing.text and "abrir en Slack" in listing.text
    key = curator.chat_conversations("C1", 7)[0]["conversation"].key
    imported = client.post("/chat/import", data={"keys": [key], "channel": "C1", "days": "7"})
    assert imported.url.path == "/" and "Importando 1 conversación del chat" in imported.text
    assert "1 conversación importada; se analiza en cola." in [e["text"] for e in client.get("/activity").json()["events"]]
    assert "Importada" in client.get("/chat", params={"channel": "C1", "days": 7}).text
    assert "Selecciona al menos" in client.post("/chat/import", data={"channel": "C1", "days": "7"}).text

    curator.chat = None
    assert "El chat no está configurado" in client.get("/chat").text
