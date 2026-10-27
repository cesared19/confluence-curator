"""Cliente mínimo de Jira Cloud (API REST v3) y conversión de tickets a texto para las notas."""

import re
from dataclasses import dataclass, field

import httpx

from .confluence import ConfluenceError

SEARCH_FIELDS = ["summary", "status", "issuetype", "updated"]
ISSUE_FIELDS = [
    "summary", "status", "issuetype", "priority", "labels", "components", "fixVersions",
    "created", "updated", "reporter", "assignee", "description", "attachment",
]
IMAGE_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}
KEY = re.compile(r"^[A-Z][A-Z0-9_]+-\d+$")


class JiraError(ConfluenceError):
    pass


@dataclass
class IssueSummary:
    key: str
    summary: str
    status: str
    issue_type: str
    updated: str


@dataclass
class Issue:
    key: str
    url: str
    updated: str
    text: str
    images: list[tuple[str, str, bytes]] = field(default_factory=list)


def to_jql(query: str) -> str:
    """Acepta JQL o una lista de claves ("SIM-1, SIM-2") y devuelve JQL."""
    keys = [k for k in re.split(r"[\s,;]+", query.strip().upper()) if k]
    if keys and all(KEY.match(k) for k in keys):
        return f"key in ({', '.join(keys)}) ORDER BY key"
    return query.strip()


def adf_to_text(node) -> str:
    """Convierte el Atlassian Document Format (descripciones y comentarios) a texto plano legible."""
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return "".join(adf_to_text(n) for n in node)
    kind = node.get("type")
    attrs = node.get("attrs") or {}
    content = node.get("content") or []
    if kind == "text":
        text = node.get("text", "")
        link = next((m["attrs"].get("href") for m in node.get("marks", []) if m.get("type") == "link"), None)
        return f"{text} ({link})" if link and link != text else text
    if kind == "hardBreak":
        return "\n"
    if kind in ("mention", "emoji", "date", "status"):
        return attrs.get("text") or attrs.get("shortName") or attrs.get("timestamp") or ""
    if kind in ("inlineCard", "blockCard", "embedCard"):
        return attrs.get("url", "")
    if kind in ("media", "mediaSingle", "mediaGroup", "mediaInline"):
        return "[imagen o archivo adjunto]\n" if kind != "mediaInline" else "[adjunto]"
    if kind in ("bulletList", "orderedList"):
        lines = []
        for index, item in enumerate(content, start=1):
            marker = f"{index}." if kind == "orderedList" else "-"
            body = adf_to_text(item).strip().replace("\n", "\n  ")
            lines.append(f"{marker} {body}")
        return "\n".join(lines) + "\n\n"
    if kind == "tableRow":
        return " | ".join(adf_to_text(cell).strip() for cell in content) + "\n"
    if kind == "codeBlock":
        return "```\n" + adf_to_text(content) + "\n```\n\n"
    if kind == "rule":
        return "---\n\n"
    inner = adf_to_text(content)
    if kind in ("paragraph", "heading", "blockquote", "panel", "table", "expand", "nestedExpand"):
        return inner.rstrip() + "\n\n"
    return inner


def _name(value) -> str:
    return (value or {}).get("displayName") or (value or {}).get("name") or ""


class JiraClient:
    def __init__(self, base_url: str, email: str, api_token: str, transport: httpx.BaseTransport | None = None):
        self.base_url = base_url.rstrip("/")
        self.http = httpx.Client(
            base_url=self.base_url,
            auth=(email, api_token),
            headers={"Accept": "application/json"},
            timeout=60,
            follow_redirects=True,
            transport=transport,
        )

    def issue_url(self, key: str) -> str:
        return f"{self.base_url}/browse/{key}"

    def _check(self, response: httpx.Response) -> httpx.Response:
        if response.is_success:
            return response
        try:
            data = response.json()
            detail = "; ".join(data.get("errorMessages", []) + list(data.get("errors", {}).values())) or response.text
        except ValueError:
            detail = response.text
        raise JiraError(f"Jira respondió {response.status_code}: {detail[:500]}", response.status_code)

    def search(self, query: str, limit: int = 50) -> list[IssueSummary]:
        payload = {"jql": to_jql(query), "fields": SEARCH_FIELDS, "maxResults": min(limit, 100)}
        issues: list[IssueSummary] = []
        while True:
            data = self._check(self.http.post("/rest/api/3/search/jql", json=payload)).json()
            for item in data.get("issues", []):
                f = item.get("fields", {})
                issues.append(
                    IssueSummary(
                        key=item["key"],
                        summary=f.get("summary", ""),
                        status=_name(f.get("status")),
                        issue_type=_name(f.get("issuetype")),
                        updated=f.get("updated", ""),
                    )
                )
            token = data.get("nextPageToken")
            if data.get("isLast", not token) or not token or len(issues) >= limit:
                return issues[:limit]
            payload["nextPageToken"] = token

    def comments(self, key: str) -> list[dict]:
        comments, start = [], 0
        while True:
            data = self._check(
                self.http.get(f"/rest/api/3/issue/{key}/comment", params={"startAt": start, "maxResults": 100, "orderBy": "created"})
            ).json()
            batch = data.get("comments", [])
            comments.extend(batch)
            start += len(batch)
            if not batch or start >= data.get("total", 0):
                return comments

    def issue(self, key: str, max_images: int, max_image_bytes: int) -> Issue:
        data = self._check(self.http.get(f"/rest/api/3/issue/{key}", params={"fields": ",".join(ISSUE_FIELDS)})).json()
        f = data.get("fields", {})
        lines = [f"{data['key']}: {f.get('summary', '')}"]
        details = [
            ("Tipo", _name(f.get("issuetype"))),
            ("Estado", _name(f.get("status"))),
            ("Prioridad", _name(f.get("priority"))),
            ("Etiquetas", ", ".join(f.get("labels") or [])),
            ("Componentes", ", ".join(_name(c) for c in f.get("components") or [])),
            ("Versiones", ", ".join(_name(v) for v in f.get("fixVersions") or [])),
            ("Reportado por", _name(f.get("reporter"))),
            ("Asignado a", _name(f.get("assignee"))),
            ("Creado", (f.get("created") or "")[:10]),
            ("Actualizado", (f.get("updated") or "")[:10]),
        ]
        lines += [f"{label}: {value}" for label, value in details if value]
        description = adf_to_text(f.get("description")).strip()
        lines += ["", "Descripción:", description or "(sin descripción)"]
        comments = self.comments(data["key"])
        if comments:
            lines += ["", "Comentarios:"]
            for c in comments:
                body = adf_to_text(c.get("body")).strip().replace("\n", "\n  ")
                lines.append(f"- {_name(c.get('author'))} ({(c.get('created') or '')[:10]}): {body}")

        images = []
        for attachment in f.get("attachment") or []:
            if len(images) >= max_images:
                break
            if attachment.get("mimeType") not in IMAGE_TYPES or attachment.get("size", 0) > max_image_bytes:
                continue
            content = self._check(self.http.get(attachment["content"])).content
            images.append((attachment.get("filename", "imagen"), attachment["mimeType"], content))
        skipped = len([a for a in f.get("attachment") or [] if a.get("mimeType") in IMAGE_TYPES]) - len(images)
        if skipped > 0:
            lines += ["", f"(Se omitieron {skipped} imágenes adjuntas por límite de cantidad o tamaño.)"]
        return Issue(
            key=data["key"],
            url=self.issue_url(data["key"]),
            updated=f.get("updated", ""),
            text="\n".join(lines).strip(),
            images=images,
        )
