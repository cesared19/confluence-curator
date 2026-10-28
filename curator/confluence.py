"""Cliente mínimo de Confluence Cloud (API REST v2, y v1 para adjuntos)."""

from dataclasses import dataclass

import httpx


class ConfluenceError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


@dataclass
class Page:
    id: str
    title: str
    parent_id: str | None
    version: int
    body: str
    updated: str = ""  # fecha ISO de la última versión


class ConfluenceClient:
    def __init__(self, base_url: str, email: str, api_token: str, transport: httpx.BaseTransport | None = None):
        self.base_url = base_url.rstrip("/")
        self.http = httpx.Client(
            base_url=self.base_url,
            auth=(email, api_token),
            headers={"Accept": "application/json"},
            timeout=60,
            transport=transport,
        )

    def page_url(self, page_id: str) -> str:
        return f"{self.base_url}/wiki/pages/viewpage.action?pageId={page_id}"

    def _check(self, response: httpx.Response) -> httpx.Response:
        if response.is_success:
            return response
        try:
            data = response.json()
            detail = data.get("message") or "; ".join(e.get("title", "") for e in data.get("errors", [])) or response.text
        except ValueError:
            detail = response.text
        raise ConfluenceError(f"Confluence respondió {response.status_code}: {detail[:500]}", response.status_code)

    def space_id(self, space_key: str) -> str:
        data = self._check(self.http.get("/wiki/api/v2/spaces", params={"keys": space_key})).json()
        results = data.get("results", [])
        if not results:
            raise ConfluenceError(f"No existe el espacio {space_key} o la cuenta no tiene acceso")
        return str(results[0]["id"])

    def list_spaces(self) -> list[tuple[str, str]]:
        """Espacios a los que la cuenta tiene acceso: (clave, nombre), ordenados por nombre."""
        spaces: list[tuple[str, str]] = []
        url: str | None = "/wiki/api/v2/spaces"
        params: dict | None = {"limit": 250, "status": "current"}
        while url:
            data = self._check(self.http.get(url, params=params)).json()
            spaces += [(item["key"], item.get("name") or item["key"]) for item in data.get("results", [])]
            url = data.get("_links", {}).get("next")
            params = None
        return sorted(spaces, key=lambda s: s[1].lower())

    def list_pages(self, space_id: str) -> list[Page]:
        pages: list[Page] = []
        url: str | None = f"/wiki/api/v2/spaces/{space_id}/pages"
        params: dict | None = {"body-format": "storage", "limit": 100, "status": "current"}
        while url:
            data = self._check(self.http.get(url, params=params)).json()
            pages.extend(self._page(item) for item in data.get("results", []))
            url = data.get("_links", {}).get("next")
            params = None
        return pages

    def get_page(self, page_id: str) -> Page:
        data = self._check(self.http.get(f"/wiki/api/v2/pages/{page_id}", params={"body-format": "storage"})).json()
        return self._page(data)

    def update_page(self, page: Page, body: str, message: str) -> Page:
        payload = {
            "id": page.id,
            "status": "current",
            "title": page.title,
            "body": {"representation": "storage", "value": body},
            "version": {"number": page.version + 1, "message": message[:250]},
        }
        self._check(self.http.put(f"/wiki/api/v2/pages/{page.id}", json=payload))
        return self.get_page(page.id)

    def create_page(self, space_id: str, parent_id: str | None, title: str, body: str) -> Page:
        payload = {
            "spaceId": space_id,
            "status": "current",
            "title": title,
            "body": {"representation": "storage", "value": body},
        }
        if parent_id:
            payload["parentId"] = parent_id
        data = self._check(self.http.post("/wiki/api/v2/pages", json=payload)).json()
        return self.get_page(str(data["id"]))

    def upload_attachment(self, page_id: str, filename: str, content: bytes, mime: str, comment: str) -> None:
        self._check(
            self.http.put(
                f"/wiki/rest/api/content/{page_id}/child/attachment",
                headers={"X-Atlassian-Token": "nocheck"},
                files={"file": (filename, content, mime)},
                data={"comment": comment[:250], "minorEdit": "true"},
            )
        )

    @staticmethod
    def _page(item: dict) -> Page:
        return Page(
            id=str(item["id"]),
            title=item.get("title", ""),
            parent_id=str(item["parentId"]) if item.get("parentId") else None,
            version=int(item.get("version", {}).get("number", 1)),
            body=item.get("body", {}).get("storage", {}).get("value", ""),
            updated=item.get("version", {}).get("createdAt", ""),
        )
