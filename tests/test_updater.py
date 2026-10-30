import io
import json
import os
import zipfile
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

import curator.update_routes as update_routes
from curator.advisor import MockAdvisor
from curator.config import Settings
from curator.db import Database
from curator.demo import FakeConfluence
from curator.service import Curator
from curator.updater import Release, UpdateError, Updater, install_kind, parse_version
from curator.web import create_app

ROOT = "cesared19-confluence-curator-1a2b3c4"


def release_zip(files: dict[str, tuple[str, int]], root: str = ROOT) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(zipfile.ZipInfo(f"{root}/"), "")
        for name, (content, mode) in files.items():
            info = zipfile.ZipInfo(f"{root}/{name}")
            info.external_attr = (0o100000 | mode) << 16
            archive.writestr(info, content)
    return buffer.getvalue()


NEW_RELEASE = release_zip({
    "curator/__init__.py": ('__version__ = "1.0.0-beta"\n', 0o644),
    "curator/nuevo.py": ("X = 1\n", 0o644),
    "README.md": ("# Nueva\n", 0o644),
    "requirements.txt": ("fastapi\n", 0o644),
    "scripts/iniciar.sh": ("#!/bin/bash\necho nuevo\n", 0o755),
})


def github(zip_bytes: bytes = NEW_RELEASE, status: int = 200, seen: list | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        if request.url.path == "/repos/cesared19/confluence-curator/releases":
            if status != 200:
                return httpx.Response(status, json={"message": "Not Found"})
            return httpx.Response(200, json=[
                {"tag_name": "v0.0.5-beta", "name": "0.0.5", "body": "", "prerelease": True, "draft": False,
                 "published_at": "2026-10-30T00:00:00Z", "zipball_url": "https://api.github.com/zip/v0.0.5-beta"},
                {"tag_name": "v1.0.0-beta", "name": "Confluence Curator 1.0.0-beta", "body": "Novedades", "prerelease": True,
                 "draft": False, "published_at": "2026-10-31T00:00:00Z", "zipball_url": "https://api.github.com/zip/v1.0.0-beta"},
                {"tag_name": "v2.0.0", "name": "borrador", "body": "", "prerelease": False, "draft": True,
                 "published_at": "", "zipball_url": "https://api.github.com/zip/v2.0.0"},
            ])
        if request.url.host == "codeload.github.com":
            return httpx.Response(200, content=zip_bytes)
        if request.url.path == "/zip/v1.0.0-beta":
            return httpx.Response(302, headers={"location": "https://codeload.github.com/zip/v1.0.0-beta"})
        return httpx.Response(404)

    return httpx.MockTransport(handler)


@pytest.fixture
def app_dir(tmp_path) -> Path:
    """Una instalación descargada de la versión 0.0.5 con la configuración y los datos del usuario."""
    root = tmp_path / "confluence-curator-0.0.5-beta"
    (root / "curator").mkdir(parents=True)
    (root / "curator" / "__init__.py").write_text('__version__ = "0.0.5-beta"\n')
    (root / "curator" / "viejo.py").write_text("Y = 2\n")
    (root / "README.md").write_text("# Vieja\n")
    (root / ".env").write_text("CONFLUENCE_EMAIL=yo@acme.com\n")
    (root / "data").mkdir()
    (root / "data" / "curator.db").write_bytes(b"base de datos")
    (root / ".venv" / "bin").mkdir(parents=True)
    (root / ".venv" / "bin" / "python").write_text("python")
    return root


def make_updater(app_dir: Path, transport=None, **kwargs) -> tuple[Updater, dict]:
    calls = {"installed": [], "restarted": 0}
    updater = Updater(
        "cesared19/confluence-curator", app_dir=app_dir, current="0.0.5-beta", transport=transport or github(),
        installer=lambda requirements: calls["installed"].append(requirements),
        restarter=lambda: calls.__setitem__("restarted", calls["restarted"] + 1),
        **kwargs,
    )
    return updater, calls


def test_version_order():
    order = ["0.0.4-beta", "0.0.5-beta", "1.0.0-beta", "1.0.0", "1.0.1-beta", "v1.2.0"]
    assert sorted(order, key=parse_version) == order
    assert parse_version("cualquier-cosa") == (-1,)


def test_releases_and_errors(app_dir):
    seen = []
    updater, _ = make_updater(app_dir, github(seen=seen), token="github_pat_x")
    releases = updater.releases()
    assert [r.tag for r in releases] == ["v1.0.0-beta", "v0.0.5-beta"]  # sin borradores, la más nueva primero
    assert updater.newer().version == "1.0.0-beta"
    assert seen[0].headers["authorization"] == "Bearer github_pat_x"

    private, _ = make_updater(app_dir, github(status=404))
    with pytest.raises(UpdateError, match="Si es privado, agrega un token"):
        private.releases()
    rejected, _ = make_updater(app_dir, github(status=401), token="malo")
    with pytest.raises(UpdateError, match="rechazó el token"):
        rejected.releases()


def test_update_replaces_app_and_keeps_user_files(app_dir):
    updater, calls = make_updater(app_dir)
    assert updater.update(updater.newer()) == "Actualizado a 1.0.0-beta."

    assert (app_dir / "curator" / "__init__.py").read_text() == '__version__ = "1.0.0-beta"\n'
    assert (app_dir / "curator" / "nuevo.py").exists() and not (app_dir / "curator" / "viejo.py").exists()
    assert (app_dir / "README.md").read_text() == "# Nueva\n"
    assert os.stat(app_dir / "scripts" / "iniciar.sh").st_mode & 0o777 == 0o755
    # Lo del usuario no se toca.
    assert (app_dir / ".env").read_text() == "CONFLUENCE_EMAIL=yo@acme.com\n"
    assert (app_dir / "data" / "curator.db").read_bytes() == b"base de datos"
    assert (app_dir / ".venv" / "bin" / "python").exists()
    # Respaldo de la versión anterior y de la base de datos.
    [backup] = updater.backups()
    assert backup.name.endswith("_0.0.5-beta")
    assert (backup / "app" / "curator" / "viejo.py").exists() and (backup / "curator.db").read_bytes() == b"base de datos"
    assert not (backup / "app" / ".env").exists()
    assert calls == {"installed": [app_dir / "requirements.txt"], "restarted": 1}
    assert not (app_dir / "backups" / ".descarga").exists()


def test_failed_update_restores_previous_version(app_dir):
    updater, calls = make_updater(app_dir)

    def broken(requirements):
        raise UpdateError("sin internet")

    updater.install_requirements = broken
    with pytest.raises(UpdateError, match="se dejó la versión 0.0.5-beta: sin internet"):
        updater.update(updater.newer())
    assert (app_dir / "curator" / "__init__.py").read_text() == '__version__ = "0.0.5-beta"\n'
    assert (app_dir / "curator" / "viejo.py").exists() and not (app_dir / "curator" / "nuevo.py").exists()
    assert calls["restarted"] == 0


def test_rollback(app_dir):
    updater, calls = make_updater(app_dir)
    updater.update(updater.newer())
    assert updater.rollback() == "Se volvió a la versión 0.0.5-beta."
    assert (app_dir / "curator" / "__init__.py").read_text() == '__version__ = "0.0.5-beta"\n'
    assert (app_dir / "curator" / "viejo.py").exists() and not (app_dir / "curator" / "nuevo.py").exists()
    assert (app_dir / ".env").exists() and calls["restarted"] == 2


def test_rejects_bad_downloads(app_dir):
    for zip_bytes, expected in [
        (release_zip({"../../afuera.txt": ("x", 0o644)}), "fuera de la carpeta"),
        (release_zip({"otra-cosa.txt": ("x", 0o644)}), "no parece una versión"),
        (b"no es un zip", "no es un zip"),
    ]:
        updater, _ = make_updater(app_dir, github(zip_bytes))
        with pytest.raises(UpdateError, match=expected):
            updater.update(updater.newer())
    assert (app_dir / "curator" / "__init__.py").read_text() == '__version__ = "0.0.5-beta"\n'


def test_only_folder_installs_update_themselves(app_dir, monkeypatch):
    assert install_kind(app_dir) == "folder"
    (app_dir / ".git").mkdir()
    assert install_kind(app_dir) == "git"
    updater, _ = make_updater(app_dir)
    with pytest.raises(UpdateError, match="Docker o copia de git"):
        updater.update(Release("v1.0.0-beta", "", "", "", True, "https://api.github.com/zip/v1.0.0-beta"))
    monkeypatch.setenv("CURATOR_IN_DOCKER", "1")
    assert install_kind(app_dir) == "docker"


def test_old_backups_are_pruned(app_dir):
    updater, _ = make_updater(app_dir)
    for index in range(5):
        (app_dir / "backups" / f"2026010{index}-000000_0.0.{index}-beta" / "app").mkdir(parents=True)
    updater.update(updater.newer())
    assert len(updater.backups()) == 3


def test_update_page_flow(app_dir, monkeypatch):
    fake_updater, calls = make_updater(app_dir)
    monkeypatch.setattr(update_routes, "Updater", lambda *args, **kwargs: fake_updater)
    monkeypatch.setattr(update_routes, "install_kind", lambda: "folder")
    monkeypatch.setattr(update_routes, "__version__", "0.0.5-beta")  # la instalación que se actualiza
    curator = Curator(Database(":memory:"), MockAdvisor(), FakeConfluence(), "DEMO", background=False)
    curator.sync()
    client = TestClient(create_app(curator, Settings()), client=("127.0.0.1", 1))

    checked = client.post("/update/check")
    assert "Hay una versión nueva: 1.0.0-beta" in checked.text
    page = client.get("/update").text
    assert "Actualizar a 1.0.0-beta" in page and "Novedades" in page
    assert "Hay una versión nueva de Confluence Curator" in client.get("/").text  # aviso en las demás páginas

    started = client.post("/update/install")
    assert started.url.path == "/update" and "updating=1" in str(started.url)
    assert calls["restarted"] == 1 and (app_dir / "curator" / "nuevo.py").exists()
    assert client.get("/activity").json()["events"][0]["text"] == "Actualizado a 1.0.0-beta."
    assert "Volver a 0.0.5-beta" in client.get("/update").text
    assert client.get("/version").json() == {"version": "0.0.5-beta"}

    remote = TestClient(create_app(curator, Settings()), client=("192.168.1.5", 1))
    assert remote.get("/update").status_code == 403 and remote.post("/update/install").status_code == 403


def test_update_token_is_saved(tmp_path, monkeypatch):
    monkeypatch.setenv("CURATOR_ENV_FILE", str(tmp_path / "config.env"))
    curator = Curator(Database(":memory:"), MockAdvisor(), FakeConfluence(), "DEMO", background=False)
    settings = Settings()
    client = TestClient(create_app(curator, settings), client=("127.0.0.1", 1))
    assert "Token guardado" in client.post("/update/token", data={"token": "github_pat_abc"}).text
    assert settings.update_token == "github_pat_abc"
    assert "CURATOR_UPDATE_TOKEN=github_pat_abc" in (tmp_path / "config.env").read_text()
    assert "github_pat_abc" not in client.get("/update").text
    client.post("/update/token", data={"remove": "1"})
    assert settings.update_token == "" and "CURATOR_UPDATE_TOKEN" not in (tmp_path / "config.env").read_text()
    os.environ.pop("CURATOR_UPDATE_TOKEN", None)
