"""Registro de lo que la herramienta está haciendo, para mostrarlo en vivo y poder detenerlo.

Cada trabajo (analizar una nota, importar tickets, revisar la documentación...) es una tarea con su avance. Detener
sube un número de "generación": las tareas iniciadas antes la consultan en sus puntos de control y se cancelan, y
los análisis que esperaban en cola se descartan. Las tareas que se inicien después funcionan con normalidad.
"""

import itertools
import threading
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone


class Cancelled(Exception):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Task:
    id: int
    kind: str
    label: str
    generation: int
    link: str = ""
    detail: str = ""
    done: int = 0
    total: int = 0
    started: str = field(default_factory=_now)
    result: str = ""  # mensaje al terminar
    result_link: str = ""
    stopped: bool = False  # se cortó por Detener
    owner: "Activity | None" = None

    def update(self, detail: str | None = None, done: int | None = None, total: int | None = None) -> None:
        if detail is not None:
            self.detail = detail
        if done is not None:
            self.done = done
        if total is not None:
            self.total = total

    def cancelled(self) -> bool:
        return self.owner is not None and self.owner.generation != self.generation

    def check(self) -> None:
        """Punto de control: si el usuario pidió detener, corta la tarea."""
        if self.cancelled():
            raise Cancelled()

    def as_dict(self) -> dict:
        return {
            "id": self.id, "kind": self.kind, "label": self.label, "detail": self.detail, "done": self.done,
            "total": self.total, "started": self.started, "link": self.link, "stopping": self.cancelled(),
        }


class Activity:
    def __init__(self):
        self.lock = threading.Lock()
        self.tasks: dict[int, Task] = {}
        self.events: deque[dict] = deque(maxlen=30)
        self.generation = 0
        self.ids = itertools.count(1)

    def event(self, text: str, level: str = "ok", link: str = "") -> None:
        with self.lock:
            self.events.appendleft({"time": _now(), "text": text, "level": level, "link": link})

    @contextmanager
    def task(self, kind: str, label: str, link: str = "", generation: int | None = None):
        """Registra una tarea mientras dura. Cancelled se convierte en un aviso; otros errores se anotan y se propagan."""
        task = Task(next(self.ids), kind, label, self.generation if generation is None else generation, link, owner=self)
        with self.lock:
            self.tasks[task.id] = task
        try:
            yield task  # el trabajo llama a task.check() en sus puntos de control
        except Cancelled:
            task.stopped = True
            self.event(f"Detenido: {label}", "warn", link)
        except Exception as error:
            self.event(f"Error en «{label}»: {error}", "error", link)
            raise
        else:
            if task.result:
                self.event(task.result, "ok", task.result_link or link)
        finally:
            with self.lock:
                self.tasks.pop(task.id, None)

    def stop(self) -> int:
        """Cancela las tareas en curso; devuelve cuántas había."""
        with self.lock:
            self.generation += 1
            return len(self.tasks)

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "tasks": [t.as_dict() for t in sorted(self.tasks.values(), key=lambda t: t.id)],
                "events": list(self.events)[:8],
            }
