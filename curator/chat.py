"""Importación desde el chat del equipo.

La interfaz y el servicio trabajan con estas estructuras genéricas; cada chat (hoy Slack) es un "origen" que
sabe listar canales, agrupar los mensajes en conversaciones y traer una conversación completa con sus imágenes.
Para sumar otro chat basta con escribir un origen con los mismos métodos que SlackSource.
"""

import html
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import httpx

from .confluence import ConfluenceError

IMAGE_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}
GAP = timedelta(minutes=30)  # mensajes sueltos más separados que esto son conversaciones distintas
MAX_MESSAGES = 2000
# Tipos de mensaje de Slack que no aportan contenido (alguien entró al canal, cambió el tema...).
IGNORED_SUBTYPES = {
    "channel_join", "channel_leave", "channel_topic", "channel_purpose", "channel_name", "channel_archive",
    "channel_unarchive", "group_join", "group_leave", "pinned_item", "unpinned_item", "bot_add", "bot_remove",
    "reminder_add", "tombstone",
}


class ChatError(ConfluenceError):
    pass


@dataclass
class Channel:
    id: str
    name: str
    private: bool = False


@dataclass
class Message:
    author: str
    ts: str
    text: str
    files: list[dict] = field(default_factory=list)

    @property
    def time(self) -> datetime:
        return datetime.fromtimestamp(float(self.ts), timezone.utc)


@dataclass
class Conversation:
    key: str  # canal:ts del primer mensaje
    channel: Channel
    thread: bool
    messages: list[Message]
    reply_count: int = 0
    last_ts: str = ""
    link: str = ""

    @property
    def start(self) -> datetime:
        return self.messages[0].time

    @property
    def preview(self) -> str:
        return " ".join(m.text for m in self.messages)[:240]

    @property
    def authors(self) -> list[str]:
        return list(dict.fromkeys(m.author for m in self.messages))

    @property
    def size(self) -> int:
        return self.reply_count + 1 if self.thread else len(self.messages)


def conversation_text(conversation: Conversation, source: str) -> str:
    """Texto de la nota: canal, fecha, mensajes con autor y hora, y enlace."""
    start = conversation.start
    kind = "Hilo" if conversation.thread else "Conversación"
    lines = [f"{kind} en {source} #{conversation.channel.name}, {start:%Y-%m-%d} (horas en UTC)", ""]
    for message in conversation.messages:
        body = message.text.replace("\n", "\n  ")
        attached = [f["name"] for f in message.files]
        if attached:
            body += f" [adjuntó: {', '.join(attached)}]"
        lines.append(f"- {message.author} ({message.time:%H:%M}): {body}")
    if conversation.link:
        lines += ["", f"Enlace: {conversation.link}"]
    return "\n".join(lines)


def group(channel: Channel, messages: list[dict], to_message, link) -> list[Conversation]:
    """Cada hilo es una conversación; los mensajes sueltos se agrupan si están a menos de 30 minutos."""
    conversations: list[Conversation] = []
    loose: list[Message] = []

    def close_loose() -> None:
        if loose:
            conversations.append(
                Conversation(f"{channel.id}:{loose[0].ts}", channel, False, list(loose), last_ts=loose[-1].ts, link=link(loose[0].ts))
            )
            loose.clear()

    for raw in sorted(messages, key=lambda m: float(m["ts"])):
        message = to_message(raw)
        if message is None:
            continue
        if raw.get("reply_count"):
            close_loose()
            conversations.append(
                Conversation(
                    f"{channel.id}:{raw['ts']}", channel, True, [message], raw["reply_count"],
                    raw.get("latest_reply", raw["ts"]), link(raw["ts"]),
                )
            )
            continue
        if loose and message.time - loose[-1].time > GAP:
            close_loose()
        loose.append(message)
    close_loose()
    return sorted(conversations, key=lambda c: c.start, reverse=True)


class SlackSource:
    """Slack con el token de una app interna del espacio de trabajo (de bot xoxb- o de usuario xoxp-)."""

    name = "Slack"

    def __init__(self, token: str, transport: httpx.BaseTransport | None = None, sleep=time.sleep):
        self.http = httpx.Client(
            base_url="https://slack.com/api",
            headers={"Authorization": f"Bearer {token}"},
            timeout=60,
            follow_redirects=True,
            transport=transport,
        )
        self.sleep = sleep
        self.users: dict[str, str] = {}
        self.workspace_url = ""

    # API --------------------------------------------------------------------

    def _call(self, method: str, **params) -> dict:
        for attempt in range(5):
            try:
                response = self.http.get(f"/{method}", params=params)
            except httpx.HTTPError as error:
                raise ChatError(f"No se pudo conectar con Slack: {error}") from error
            if response.status_code == 429 and attempt < 4:
                self.sleep(int(response.headers.get("Retry-After", "5")))
                continue
            if not response.is_success:
                raise ChatError(f"Slack respondió {response.status_code}", response.status_code)
            data = response.json()
            if data.get("ok"):
                return data
            raise ChatError(self._explain(data), 401 if data.get("error") in ("invalid_auth", "not_authed", "token_revoked") else 400)
        raise ChatError("Slack sigue limitando las consultas; inténtalo en unos minutos.", 429)

    @staticmethod
    def _explain(data: dict) -> str:
        error = data.get("error", "error desconocido")
        messages = {
            "invalid_auth": "Slack rechazó el token. Revisa que esté copiado completo.",
            "not_authed": "Falta el token de Slack.",
            "token_revoked": "Ese token de Slack fue revocado; crea uno nuevo.",
            "not_in_channel": "La app no está en ese canal: en Slack, escribe /invite @nombre-de-la-app en el canal.",
            "channel_not_found": "No se encontró el canal o la app no tiene acceso a él.",
        }
        if error == "missing_scope":
            return f"Al token le falta el permiso {data.get('needed', '')} de Slack. Agrégalo en la app y reinstálala."
        return messages.get(error, f"Slack respondió: {error}")

    def check(self) -> str:
        data = self._call("auth.test")
        self.workspace_url = data.get("url", "").rstrip("/")
        return f"{data.get('team', 'espacio de trabajo')} como {data.get('user', '?')}"

    def channels(self) -> list[Channel]:
        channels, cursor = [], ""
        while True:
            data = self._call(
                "conversations.list", types="public_channel,private_channel", exclude_archived="true", limit=200, cursor=cursor
            )
            channels += [
                Channel(c["id"], c.get("name", c["id"]), c.get("is_private", False))
                for c in data.get("channels", [])
                if c.get("is_member", True)
            ]
            cursor = data.get("response_metadata", {}).get("next_cursor", "")
            if not cursor:
                return sorted(channels, key=lambda c: c.name)

    def _history(self, channel: str, oldest: float) -> list[dict]:
        messages, cursor = [], ""
        while len(messages) < MAX_MESSAGES:
            data = self._call("conversations.history", channel=channel, oldest=f"{oldest:.6f}", limit=200, cursor=cursor)
            messages += data.get("messages", [])
            cursor = data.get("response_metadata", {}).get("next_cursor", "")
            if not cursor or not data.get("has_more", bool(cursor)):
                break
        return messages

    def _user(self, user_id: str) -> str:
        if user_id not in self.users:
            try:
                profile = self._call("users.info", user=user_id).get("user", {})
                self.users[user_id] = (
                    profile.get("profile", {}).get("display_name") or profile.get("real_name") or profile.get("name") or user_id
                )
            except ChatError:
                self.users[user_id] = user_id
        return self.users[user_id]

    def _text(self, text: str) -> str:
        """Convierte el formato de Slack (<@U1>, <#C1|canal>, <url|texto>) a texto legible."""
        text = re.sub(r"<@([UW][A-Z0-9]+)(?:\|[^>]*)?>", lambda m: "@" + self._user(m.group(1)), text)
        text = re.sub(r"<#[CG][A-Z0-9]+\|([^>]*)>", r"#\1", text)
        text = re.sub(r"<!(here|channel|everyone)>", r"@\1", text)
        text = re.sub(r"<(https?://[^|>]+)\|([^>]+)>", r"\2 (\1)", text)
        text = re.sub(r"<(https?://[^>]+)>", r"\1", text)
        return html.unescape(text)

    def _message(self, raw: dict) -> Message | None:
        if raw.get("subtype") in IGNORED_SUBTYPES:
            return None
        author = self._user(raw["user"]) if raw.get("user") else raw.get("username") or raw.get("bot_profile", {}).get("name", "bot")
        files = [
            {"name": f.get("name", "archivo"), "mimetype": f.get("mimetype", ""), "size": f.get("size", 0),
             "url": f.get("url_private_download") or f.get("url_private", "")}
            for f in raw.get("files", [])
        ]
        text = self._text(raw.get("text", ""))
        if not text.strip() and not files:
            return None
        return Message(author, raw["ts"], text, files)

    def _link(self, channel: str, ts: str) -> str:
        if not self.workspace_url:
            try:
                self.check()
            except ChatError:
                return ""
        return f"{self.workspace_url}/archives/{channel}/p{ts.replace('.', '')}"

    # Interfaz genérica ---------------------------------------------------------

    def conversations(self, channel: Channel, days: int) -> list[Conversation]:
        oldest = (datetime.now(timezone.utc) - timedelta(days=days)).timestamp()
        raw = [m for m in self._history(channel.id, oldest) if m.get("thread_ts", m["ts"]) == m["ts"]]
        return group(channel, raw, self._message, lambda ts: self._link(channel.id, ts))

    def complete(self, conversation: Conversation) -> Conversation:
        """Trae las respuestas de un hilo (el listado solo tiene el primer mensaje)."""
        if not conversation.thread:
            return conversation
        replies, cursor = [], ""
        while True:
            data = self._call(
                "conversations.replies", channel=conversation.channel.id, ts=conversation.messages[0].ts, limit=200, cursor=cursor
            )
            replies += data.get("messages", [])
            cursor = data.get("response_metadata", {}).get("next_cursor", "")
            if not cursor:
                break
        messages = [m for m in (self._message(r) for r in sorted(replies, key=lambda r: float(r["ts"]))) if m]
        conversation.messages = messages or conversation.messages
        conversation.last_ts = conversation.messages[-1].ts
        return conversation

    def download(self, file: dict) -> bytes:
        try:
            response = self.http.get(file["url"])
        except httpx.HTTPError as error:
            raise ChatError(f"No se pudo descargar {file['name']}: {error}") from error
        if not response.is_success or response.headers.get("content-type", "").startswith("text/html"):
            # Sin el permiso files:read, Slack responde con su página de inicio de sesión.
            raise ChatError(f"No se pudo descargar {file['name']}: al token le falta el permiso files:read.")
        return response.content
