import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS pages (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    parent_id TEXT,
    version INTEGER NOT NULL,
    body TEXT NOT NULL,
    updated TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    text TEXT NOT NULL,
    source_type TEXT NOT NULL,
    source_ref TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT '',
    no_change_reason TEXT NOT NULL DEFAULT '',
    alternatives TEXT NOT NULL DEFAULT '[]',
    error TEXT NOT NULL DEFAULT '',
    analyzing INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS jira_imports (
    issue_key TEXT NOT NULL,
    issue_updated TEXT NOT NULL,
    note_id INTEGER NOT NULL REFERENCES notes(id),
    imported_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pdf_documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    filename TEXT NOT NULL,
    title TEXT NOT NULL,
    source_type TEXT NOT NULL,
    pages INTEGER NOT NULL,
    data BLOB NOT NULL,
    plan_params TEXT NOT NULL DEFAULT '',
    plan TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pdf_imports (
    document_id INTEGER NOT NULL REFERENCES pdf_documents(id),
    chunk_key TEXT NOT NULL,
    note_id INTEGER NOT NULL REFERENCES notes(id),
    imported_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_findings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    fingerprint TEXT NOT NULL UNIQUE,
    page_id TEXT NOT NULL,
    section_index INTEGER NOT NULL,
    page_version INTEGER NOT NULL,
    other_page_id TEXT NOT NULL DEFAULT '',
    other_section_index INTEGER NOT NULL DEFAULT -1,
    other_page_version INTEGER NOT NULL DEFAULT 0,
    issue_key TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL,
    detail TEXT NOT NULL,
    suggestion TEXT NOT NULL DEFAULT '',
    confidence REAL NOT NULL DEFAULT 1,
    status TEXT NOT NULL,
    note_id INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_pairs (
    pair_hash TEXT PRIMARY KEY,
    checked_at TEXT NOT NULL,
    result TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS note_images (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    note_id INTEGER NOT NULL REFERENCES notes(id),
    idx INTEGER NOT NULL,
    filename TEXT NOT NULL,
    mime TEXT NOT NULL,
    data BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS proposals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    note_id INTEGER NOT NULL REFERENCES notes(id),
    action TEXT NOT NULL,
    page_id TEXT NOT NULL,
    section_index INTEGER NOT NULL,
    old_storage TEXT NOT NULL,
    new_storage TEXT NOT NULL,
    new_title TEXT NOT NULL,
    summary TEXT NOT NULL,
    rationale TEXT NOT NULL,
    confidence REAL NOT NULL,
    conflicts TEXT NOT NULL,
    status TEXT NOT NULL,
    result_url TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    decided_at TEXT
);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: str):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        # La conexión se comparte entre las peticiones y el análisis en segundo plano.
        self.lock = threading.RLock()
        self.conn.executescript(SCHEMA)
        # Columnas agregadas después de 0.0.1: las bases existentes se actualizan al abrirlas.
        for table, column, definition in [
            ("notes", "analyzing", "INTEGER NOT NULL DEFAULT 0"),
            ("pages", "updated", "TEXT NOT NULL DEFAULT ''"),
        ]:
            columns = {row["name"] for row in self.conn.execute(f"PRAGMA table_info({table})")}
            if column not in columns:
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self.lock, self.conn:
            return self.conn.execute(sql, params)

    def one(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        with self.lock:
            return self.conn.execute(sql, params).fetchone()

    def all(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self.lock:
            return self.conn.execute(sql, params).fetchall()

    def get_meta(self, key: str, default: str = "") -> str:
        row = self.one("SELECT value FROM meta WHERE key = ?", (key,))
        return row["value"] if row else default

    def set_meta(self, key: str, value: str) -> None:
        self.execute("INSERT INTO meta(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))

    def replace_pages(self, pages) -> None:
        with self.lock, self.conn:
            self.conn.execute("DELETE FROM pages")
            self.conn.executemany(
                "INSERT INTO pages(id, title, parent_id, version, body, updated) VALUES (?, ?, ?, ?, ?, ?)",
                [(p.id, p.title, p.parent_id, p.version, p.body, p.updated) for p in pages],
            )

    def upsert_page(self, page) -> None:
        self.execute(
            "INSERT INTO pages(id, title, parent_id, version, body, updated) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET title = excluded.title, parent_id = excluded.parent_id, "
            "version = excluded.version, body = excluded.body, updated = excluded.updated",
            (page.id, page.title, page.parent_id, page.version, page.body, page.updated),
        )


def loads(value: str) -> list:
    return json.loads(value) if value else []
