"""SQLite persistence for sessions and messages.

Uses the standard-library sqlite3 driver. Every public coroutine runs the blocking
work in a worker thread (asyncio.to_thread) with its own short-lived connection, so
the event loop is never blocked and no connection is shared across threads.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id            TEXT PRIMARY KEY,
    system_prompt TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT    NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    role       TEXT    NOT NULL CHECK (role IN ('user', 'assistant')),
    content    TEXT    NOT NULL,
    metadata   TEXT,
    created_at TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_messages_session_id_id ON messages(session_id, id);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


@dataclass(frozen=True)
class StoredMessage:
    role: str
    content: str
    metadata: dict[str, Any] | None


@dataclass(frozen=True)
class SessionRecord:
    id: str
    system_prompt: str


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path

    # -- connection helpers -------------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 15000")
        return conn

    # -- lifecycle ----------------------------------------------------------
    def _init_sync(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as conn:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.executescript(SCHEMA)

    async def init(self) -> None:
        await asyncio.to_thread(self._init_sync)

    def _ping_sync(self) -> bool:
        with closing(self._connect()) as conn:
            conn.execute("SELECT 1 FROM messages LIMIT 1").fetchall()
        return True

    async def ping(self) -> bool:
        try:
            return await asyncio.to_thread(self._ping_sync)
        except sqlite3.Error:
            return False

    # -- reads ----------------------------------------------------------------
    def _get_session_sync(self, session_id: str) -> SessionRecord | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT id, system_prompt FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
        return SessionRecord(id=row["id"], system_prompt=row["system_prompt"]) if row else None

    async def get_session(self, session_id: str) -> SessionRecord | None:
        return await asyncio.to_thread(self._get_session_sync, session_id)

    def _get_messages_sync(self, session_id: str, limit: int | None) -> list[StoredMessage]:
        with closing(self._connect()) as conn:
            if limit is None:
                rows = conn.execute(
                    "SELECT role, content, metadata FROM messages WHERE session_id = ? ORDER BY id ASC",
                    (session_id,),
                ).fetchall()
            else:
                # Most recent `limit` messages, returned oldest first.
                rows = conn.execute(
                    "SELECT role, content, metadata FROM ("
                    "  SELECT id, role, content, metadata FROM messages"
                    "  WHERE session_id = ? ORDER BY id DESC LIMIT ?"
                    ") ORDER BY id ASC",
                    (session_id, limit),
                ).fetchall()
        return [
            StoredMessage(
                role=r["role"],
                content=r["content"],
                metadata=json.loads(r["metadata"]) if r["metadata"] else None,
            )
            for r in rows
        ]

    async def get_messages(self, session_id: str, limit: int | None = None) -> list[StoredMessage]:
        return await asyncio.to_thread(self._get_messages_sync, session_id, limit)

    # -- writes ---------------------------------------------------------------
    def _save_turn_sync(
        self,
        session_id: str,
        system_prompt: str,
        user_content: str,
        assistant_content: str,
        assistant_metadata: dict[str, Any],
    ) -> None:
        """Atomically upsert the session and append the user + assistant messages."""
        now = _now()
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    "INSERT INTO sessions (id, system_prompt, created_at, updated_at) VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(id) DO UPDATE SET system_prompt = excluded.system_prompt, "
                    "updated_at = excluded.updated_at",
                    (session_id, system_prompt, now, now),
                )
                conn.execute(
                    "INSERT INTO messages (session_id, role, content, metadata, created_at) VALUES (?, 'user', ?, NULL, ?)",
                    (session_id, user_content, now),
                )
                conn.execute(
                    "INSERT INTO messages (session_id, role, content, metadata, created_at) VALUES (?, 'assistant', ?, ?, ?)",
                    (session_id, assistant_content, json.dumps(assistant_metadata), _now()),
                )
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise

    async def save_turn(
        self,
        session_id: str,
        system_prompt: str,
        user_content: str,
        assistant_content: str,
        assistant_metadata: dict[str, Any],
    ) -> None:
        await asyncio.to_thread(
            self._save_turn_sync,
            session_id,
            system_prompt,
            user_content,
            assistant_content,
            assistant_metadata,
        )
