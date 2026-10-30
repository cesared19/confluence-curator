"""Página de actualizaciones: buscar versiones nuevas, actualizar, volver atrás y el token de GitHub."""

import json
import os
from dataclasses import asdict
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, Form, Request

from . import __version__
from .audit import parse_time
from .config import save_env
from .db import now
from .setup import is_allowed
from .updater import Release, UpdateError, Updater, install_kind, parse_version

CHECK_EVERY = timedelta(hours=24)


def register(app: FastAPI, curator, settings, templates, render, back) -> None:
    state = {"updater": None}

    def updater() -> Updater:
        if state["updater"] is None:
            state["updater"] = Updater(settings.update_repo, settings.update_token, settings.update_api)
        return state["updater"]

    def check(force: bool = False) -> None:
        """Busca la última versión (una vez al día, o ya mismo con force) y la guarda para avisar."""
        db = curator.db
        last = parse_time(db.get_meta("update_checked"))
        if not force and last and datetime.now(timezone.utc) - last < CHECK_EVERY:
            return
        if install_kind() != "folder" and not force:
            return
        try:
            latest = updater().latest()
            db.set_meta("update_release", json.dumps(asdict(latest), ensure_ascii=False) if latest else "")
            db.set_meta("update_error", "")
        except UpdateError as error:
            db.set_meta("update_error", str(error))
        finally:
            db.set_meta("update_checked", now())

    curator.periodic.append(check)

    def latest_release() -> Release | None:
        raw = curator.db.get_meta("update_release")
        return Release(**json.loads(raw)) if raw else None

    def update_available() -> Release | None:
        latest = latest_release()
        return latest if latest and parse_version(latest.tag) > parse_version(__version__) else None

    templates.env.globals["update_available"] = update_available

    @app.get("/update")
    def update_page(request: Request, error: str = "", message: str = "", updating: str = ""):
        if not is_allowed(request, settings):
            return render(request, "setup_forbidden.html", status_code=403)
        return render(
            request,
            "update.html",
            current=__version__,
            kind=install_kind(),
            latest=latest_release(),
            newer=update_available(),
            checked=curator.db.get_meta("update_checked"),
            check_error=curator.db.get_meta("update_error"),
            backups=[b.name.split("_", 1)[-1] for b in updater().backups()],
            has_token=bool(settings.update_token),
            repo=settings.update_repo,
            windows=os.name == "nt",
            updating=updating,
            error=error,
            message=message,
        )

    @app.post("/update/check")
    def update_check(request: Request):
        if not is_allowed(request, settings):
            return render(request, "setup_forbidden.html", status_code=403)
        check(force=True)
        if curator.db.get_meta("update_error"):
            return back("/update", error=curator.db.get_meta("update_error"))
        newer = update_available()
        return back("/update", message=f"Hay una versión nueva: {newer.version}." if newer else "Ya tienes la última versión.")

    @app.post("/update/install")
    def update_install(request: Request):
        if not is_allowed(request, settings):
            return render(request, "setup_forbidden.html", status_code=403)
        release = update_available()
        if release is None:
            return back("/update", error="No hay una versión nueva para instalar.")
        if install_kind() != "folder":
            return back("/update", error="Esta instalación no se actualiza desde aquí.")

        def work(task):
            result = updater().update(release, progress=lambda text: task.update(detail=text))
            if os.name == "nt":
                result += " Cierra la ventana de la herramienta y vuelve a abrir el lanzador para usarla."
            return result

        curator.start_job("update", f"Actualizando a {release.version}", work, "/update")
        return back("/update?updating=1", message=f"Actualizando a {release.version}. La página se recarga sola al terminar.")

    @app.post("/update/rollback")
    def update_rollback(request: Request):
        if not is_allowed(request, settings):
            return render(request, "setup_forbidden.html", status_code=403)
        if not updater().backups():
            return back("/update", error="No hay copias de respaldo de versiones anteriores.")
        curator.start_job("update", "Volviendo a la versión anterior", lambda task: updater().rollback(), "/update")
        return back("/update?updating=1", message="Volviendo a la versión anterior. La página se recarga sola al terminar.")

    @app.post("/update/token")
    def update_token(request: Request, token: str = Form(""), remove: str = Form("")):
        if not is_allowed(request, settings):
            return render(request, "setup_forbidden.html", status_code=403)
        value = "" if remove else token.strip()
        if not value and not remove:
            return back("/update", error="Pega el token.")
        try:
            save_env({"CURATOR_UPDATE_TOKEN": value})
        except OSError as failure:
            return back("/update", error=f"No se pudo guardar el token: {failure}")
        settings.update_token = value
        state["updater"] = None
        return back("/update", message="Token quitado." if remove else "Token guardado.")

    @app.get("/version")
    def version():
        return {"version": __version__}
