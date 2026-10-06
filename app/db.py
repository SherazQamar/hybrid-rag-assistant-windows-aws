"""SQLite storage for the POC (WAL mode, parameterised SQL only).

The full project swaps this module for PostgreSQL + pgvector + full-text search on AWS
(see docs/postgres_schema.sql). Every query here is parameterised - no string-built SQL
from user input.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

_db_path: Path | None = None
_write_lock = threading.RLock()

MIGRATIONS: list[str] = [
    # 1 - initial schema
    """
    CREATE TABLE users (
        id INTEGER PRIMARY KEY,
        username TEXT NOT NULL UNIQUE COLLATE NOCASE,
        password_hash TEXT NOT NULL,
        role TEXT NOT NULL CHECK (role IN ('admin','user')),
        active INTEGER NOT NULL DEFAULT 1,
        totp_secret TEXT,
        created_at REAL NOT NULL
    );
    CREATE TABLE sessions (
        token_hash TEXT PRIMARY KEY,
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        created_at REAL NOT NULL,
        expires_at REAL NOT NULL
    );
    CREATE TABLE login_failures (
        id INTEGER PRIMARY KEY,
        username TEXT NOT NULL,
        at REAL NOT NULL
    );
    CREATE TABLE chats (
        id INTEGER PRIMARY KEY,
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        title TEXT NOT NULL,
        created_at REAL NOT NULL
    );
    CREATE TABLE messages (
        id INTEGER PRIMARY KEY,
        chat_id INTEGER NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
        role TEXT NOT NULL,
        content TEXT NOT NULL,
        sources_json TEXT,
        created_at REAL NOT NULL
    );
    CREATE TABLE memories (
        id INTEGER PRIMARY KEY,
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        content TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('pending','active')),
        origin TEXT NOT NULL,
        created_at REAL NOT NULL
    );
    CREATE TABLE documents (
        id INTEGER PRIMARY KEY,
        owner_id INTEGER NOT NULL REFERENCES users(id),
        filename TEXT NOT NULL,
        file_type TEXT NOT NULL,
        visibility TEXT NOT NULL CHECK (visibility IN ('private','shared')),
        status TEXT NOT NULL,
        error TEXT,
        sha256 TEXT NOT NULL,
        stored_name TEXT,
        size_bytes INTEGER NOT NULL,
        n_chunks INTEGER DEFAULT 0,
        created_at REAL NOT NULL,
        deleted_at REAL
    );
    CREATE TABLE index_versions (
        id INTEGER PRIMARY KEY,
        profile_name TEXT NOT NULL,
        chunking_json TEXT NOT NULL,
        embed_provider TEXT NOT NULL,
        embed_model TEXT NOT NULL,
        embed_dim INTEGER NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('building','active','failed','retired')),
        progress REAL NOT NULL DEFAULT 0,
        error TEXT,
        created_at REAL NOT NULL,
        activated_at REAL
    );
    CREATE TABLE chunks (
        id INTEGER PRIMARY KEY,
        doc_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
        index_version INTEGER NOT NULL REFERENCES index_versions(id) ON DELETE CASCADE,
        ordinal INTEGER NOT NULL,
        text TEXT NOT NULL,
        page INTEGER,
        section TEXT,
        start_char INTEGER NOT NULL,
        end_char INTEGER NOT NULL,
        n_chars INTEGER NOT NULL,
        n_tokens INTEGER NOT NULL,
        embedding BLOB NOT NULL,
        UNIQUE (doc_id, index_version, ordinal)
    );
    CREATE INDEX ix_chunks_version ON chunks(index_version);
    CREATE VIRTUAL TABLE chunks_fts USING fts5(text, chunk_id UNINDEXED, tokenize='porter unicode61');
    CREATE TABLE profiles (
        name TEXT PRIMARY KEY,
        json TEXT NOT NULL,
        updated_at REAL NOT NULL
    );
    CREATE TABLE kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    CREATE TABLE usage_log (
        id INTEGER PRIMARY KEY,
        user_id INTEGER,
        kind TEXT NOT NULL,
        detail TEXT,
        latency_ms REAL,
        ok INTEGER NOT NULL DEFAULT 1,
        created_at REAL NOT NULL
    );
    CREATE TABLE retrieval_log (
        id INTEGER PRIMARY KEY,
        user_id INTEGER,
        query TEXT NOT NULL,
        mode TEXT NOT NULL,
        debug_json TEXT NOT NULL,
        created_at REAL NOT NULL
    );
    """,
]


def init_db(path: Path) -> None:
    global _db_path
    _db_path = path
    with connect() as con:
        con.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
        row = con.execute("SELECT MAX(version) FROM schema_version").fetchone()
        current = row[0] or 0
        for i, sql in enumerate(MIGRATIONS[current:], start=current + 1):
            con.executescript(sql)
            con.execute("INSERT INTO schema_version(version) VALUES (?)", (i,))


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    assert _db_path is not None, "init_db() not called"
    con = sqlite3.connect(_db_path, timeout=30, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("PRAGMA journal_mode = WAL")
    con.execute("PRAGMA busy_timeout = 30000")
    try:
        with _write_lock:
            yield con
            con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def query(sql: str, params: tuple | list = ()) -> list[dict[str, Any]]:
    with connect() as con:
        return [dict(r) for r in con.execute(sql, params).fetchall()]


def one(sql: str, params: tuple | list = ()) -> dict[str, Any] | None:
    rows = query(sql, params)
    return rows[0] if rows else None


def execute(sql: str, params: tuple | list = ()) -> int:
    with connect() as con:
        cur = con.execute(sql, params)
        return cur.lastrowid


def kv_get(key: str, default: str | None = None) -> str | None:
    row = one("SELECT value FROM kv WHERE key = ?", (key,))
    return row["value"] if row else default


def kv_set(key: str, value: str) -> None:
    execute("INSERT INTO kv(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))


def log_usage(user_id: int | None, kind: str, detail: Any = None, latency_ms: float | None = None, ok: bool = True) -> None:
    execute(
        "INSERT INTO usage_log(user_id, kind, detail, latency_ms, ok, created_at) VALUES (?,?,?,?,?,?)",
        (user_id, kind, json.dumps(detail) if not isinstance(detail, str) else detail, latency_ms, int(ok), time.time()),
    )
