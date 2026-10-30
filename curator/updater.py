"""Actualización desde los releases de GitHub, sin descargar ni copiar carpetas a mano.

Descarga el release, guarda una copia de respaldo de la versión actual (y de la base de datos), reemplaza los
archivos de la aplicación sin tocar la configuración (.env), los datos (data/) ni el entorno de Python (.venv/),
instala las dependencias nuevas y reinicia el proceso. Cada archivo se reemplaza de forma atómica (se escribe
aparte y se renombra), así un lanzador que se está ejecutando sigue leyendo su versión anterior sin romperse.
"""

import io
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path

import httpx

from . import __version__

APP_DIR = Path(__file__).resolve().parent.parent
# Lo que pertenece al usuario y nunca se reemplaza ni se borra.
KEEP = {".env", "data", ".venv", "backups", ".git", "__pycache__", ".pytest_cache"}
# Carpetas que el release trae completas: lo que ya no venga en ellas se borra (queda en el respaldo).
MANAGED_DIRS = {"curator", "scripts", "tests", "docs"}
BACKUPS_KEPT = 3


class UpdateError(Exception):
    pass


def parse_version(text: str) -> tuple:
    """'v0.0.6-beta' → (0, 0, 6, 0, 'beta'); una versión sin sufijo va después de su beta."""
    match = re.match(r"v?(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.]+))?$", text.strip())
    if not match:
        return (-1,)
    major, minor, patch, suffix = match.groups()
    return (int(major), int(minor), int(patch), 0 if suffix else 1, suffix or "")


@dataclass
class Release:
    tag: str
    name: str
    notes: str
    published: str
    prerelease: bool
    zipball_url: str

    @property
    def version(self) -> str:
        return self.tag.lstrip("v")


def install_kind(app_dir: Path = APP_DIR) -> str:
    """'docker', 'git' o 'folder' (carpeta descargada: la única que se actualiza sola)."""
    if Path("/.dockerenv").exists() or os.environ.get("CURATOR_IN_DOCKER") == "1":
        return "docker"
    if (app_dir / ".git").exists():
        return "git"
    return "folder"


class Updater:
    def __init__(
        self,
        repo: str,
        token: str = "",
        api: str = "https://api.github.com",
        app_dir: Path = APP_DIR,
        current: str = __version__,
        transport: httpx.BaseTransport | None = None,
        installer=None,
        restarter=None,
    ):
        self.repo = repo
        self.token = token
        self.app_dir = app_dir
        self.current = current
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self.http = httpx.Client(base_url=api.rstrip("/"), headers=headers, timeout=60, follow_redirects=True, transport=transport)
        self.install_requirements = installer or self._pip_install
        self.restart = restarter or restart_process
        self.lock = threading.Lock()

    # Consultar --------------------------------------------------------------------

    def releases(self) -> list[Release]:
        try:
            response = self.http.get(f"/repos/{self.repo}/releases", params={"per_page": 20})
        except httpx.HTTPError as error:
            raise UpdateError(f"No se pudo conectar con GitHub: {error}") from error
        if response.status_code in (401, 403):
            raise UpdateError("GitHub rechazó el token: revisa que sea válido y tenga permiso de lectura sobre el repositorio.")
        if response.status_code == 404:
            raise UpdateError(
                "GitHub no encontró el repositorio. Si es privado, agrega un token de solo lectura (ver abajo)."
                if not self.token
                else "GitHub no encontró el repositorio con ese token: revisa que el token tenga acceso a él."
            )
        if not response.is_success:
            raise UpdateError(f"GitHub respondió {response.status_code}.")
        releases = [
            Release(r["tag_name"], r.get("name") or r["tag_name"], r.get("body") or "", r.get("published_at") or "",
                    r.get("prerelease", False), r["zipball_url"])
            for r in response.json()
            if not r.get("draft") and parse_version(r["tag_name"]) != (-1,)
        ]
        return sorted(releases, key=lambda r: parse_version(r.tag), reverse=True)

    def latest(self) -> Release | None:
        releases = self.releases()
        return releases[0] if releases else None

    def newer(self) -> Release | None:
        latest = self.latest()
        return latest if latest and parse_version(latest.tag) > parse_version(self.current) else None

    # Actualizar -------------------------------------------------------------------

    def update(self, release: Release, progress=lambda text: None) -> str:
        """Instala el release y reinicia. Si algo falla antes de reiniciar, deja todo como estaba."""
        if install_kind(self.app_dir) != "folder":
            raise UpdateError("Esta instalación no se actualiza desde aquí (Docker o copia de git).")
        if not self.lock.acquire(blocking=False):
            raise UpdateError("Ya hay una actualización en curso.")
        try:
            progress(f"Descargando {release.version}")
            source = self._download(release)
            progress("Guardando una copia de respaldo de la versión actual")
            backup = self._backup()
            try:
                progress("Reemplazando los archivos")
                self._install(source, self.app_dir)
                progress("Instalando dependencias")
                self.install_requirements(self.app_dir / "requirements.txt")
            except Exception as error:
                self._install_tree(backup, self.app_dir)
                raise UpdateError(f"No se pudo actualizar; se dejó la versión {self.current}: {error}") from error
            self._prune_backups()
            progress("Reiniciando")
            self.restart()
            return f"Actualizado a {release.version}."
        finally:
            self.lock.release()

    def rollback(self) -> str:
        """Vuelve a la versión del último respaldo (los datos actuales no se tocan)."""
        backups = self.backups()
        if not backups:
            raise UpdateError("No hay copias de respaldo de versiones anteriores.")
        backup = backups[0]
        self._install_tree(backup / "app", self.app_dir)
        self.install_requirements(self.app_dir / "requirements.txt")
        self.restart()
        return f"Se volvió a la versión {backup.name.split('_', 1)[-1]}."

    def backups(self) -> list[Path]:
        folder = self.app_dir / "backups"
        if not folder.is_dir():
            return []
        return sorted((p for p in folder.iterdir() if (p / "app").is_dir()), key=lambda p: p.name, reverse=True)

    def _download(self, release: Release) -> Path:
        try:
            response = self.http.get(release.zipball_url)
        except httpx.HTTPError as error:
            raise UpdateError(f"No se pudo descargar la versión {release.version}: {error}") from error
        if not response.is_success:
            raise UpdateError(f"GitHub respondió {response.status_code} al descargar la versión {release.version}.")
        staging = self.app_dir / "backups" / ".descarga"
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True)
        try:
            archive = zipfile.ZipFile(io.BytesIO(response.content))
        except zipfile.BadZipFile as error:
            raise UpdateError("La descarga no es un zip válido.") from error
        for info in archive.infolist():
            target = (staging / info.filename).resolve()
            if not str(target).startswith(str(staging.resolve())):
                raise UpdateError("El zip contiene rutas fuera de la carpeta; se cancela por seguridad.")
            extracted = archive.extract(info, staging)
            mode = info.external_attr >> 16
            if mode and not info.is_dir():
                os.chmod(extracted, mode & 0o777)  # conserva los permisos (p. ej. lanzadores ejecutables)
        roots = [p for p in staging.iterdir() if p.is_dir()]
        root = roots[0] if len(roots) == 1 else staging
        if not (root / "curator" / "__init__.py").is_file():
            raise UpdateError("La descarga no parece una versión de Confluence Curator.")
        return root

    def _backup(self) -> Path:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        backup = self.app_dir / "backups" / f"{stamp}_{self.current}"
        app_copy = backup / "app"
        app_copy.mkdir(parents=True)
        for item in self.app_dir.iterdir():
            if item.name in KEEP:
                continue
            if item.is_dir():
                shutil.copytree(item, app_copy / item.name, ignore=shutil.ignore_patterns("__pycache__"))
            else:
                shutil.copy2(item, app_copy / item.name)
        database = self.app_dir / "data" / "curator.db"
        if database.is_file():
            shutil.copy2(database, backup / "curator.db")
        return app_copy

    def _install(self, source: Path, target: Path) -> None:
        self._install_tree(source, target)

    def _install_tree(self, source: Path, target: Path) -> None:
        """Copia source sobre target archivo por archivo (reemplazo atómico) y borra lo que sobra en las carpetas
        administradas; lo del usuario (KEEP) nunca se toca."""
        incoming = set()
        for path in source.rglob("*"):
            relative = path.relative_to(source)
            if relative.parts[0] in KEEP or "__pycache__" in relative.parts:
                continue
            destination = target / relative
            if path.is_dir():
                destination.mkdir(parents=True, exist_ok=True)
                continue
            incoming.add(relative)
            destination.parent.mkdir(parents=True, exist_ok=True)
            temporary = destination.with_name(f".{destination.name}.nuevo")
            shutil.copy2(path, temporary)
            os.replace(temporary, destination)
        for folder in MANAGED_DIRS:
            for path in sorted((target / folder).rglob("*"), reverse=True) if (target / folder).is_dir() else []:
                relative = path.relative_to(target)
                if "__pycache__" in relative.parts:
                    continue
                if path.is_file() and relative not in incoming:
                    path.unlink()
                elif path.is_dir() and not any(path.iterdir()):
                    path.rmdir()

    def _prune_backups(self) -> None:
        shutil.rmtree(self.app_dir / "backups" / ".descarga", ignore_errors=True)
        for old in self.backups()[BACKUPS_KEPT:]:
            shutil.rmtree(old, ignore_errors=True)

    @staticmethod
    def _pip_install(requirements: Path) -> None:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "-q", "-r", str(requirements)],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise UpdateError(f"No se pudieron instalar las dependencias: {result.stderr.strip()[-500:]}")


def restart_process() -> None:
    """Reinicia la aplicación en el mismo proceso y la misma ventana, con los archivos nuevos.

    En Windows no se reinicia sola: el lanzador .bat no soporta bien ese reemplazo; se pide reabrirla.
    """
    if os.name == "nt":
        return

    def later() -> None:
        time.sleep(1.5)  # deja terminar la respuesta en curso
        args = [a for a in sys.argv[1:] if a != "--open"]  # el navegador ya está abierto
        os.execv(sys.executable, [sys.executable, "-m", "curator", *args])

    threading.Thread(target=later, name="curator-restart", daemon=True).start()
