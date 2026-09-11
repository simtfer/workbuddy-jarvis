"""SQLite-backed conversation history."""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id          TEXT PRIMARY KEY,
    title       TEXT NOT NULL DEFAULT '',
    model       TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT NOT NULL,
    role        TEXT NOT NULL,
    content     TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, id);
"""


class HistoryStore:
    """Stores chat turns so JARVIS remembers what happened yesterday."""

    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.path = db_path
        self.conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    # ---------------------------------------------------------------- session
    def ensure_session(self, session_id: str, model: str = "") -> None:
        now = datetime.now().isoformat(timespec="seconds")
        self.conn.execute(
            "INSERT OR IGNORE INTO sessions (id, title, model, created_at, updated_at)"
            " VALUES (?, '', ?, ?, ?)",
            (session_id, model, now, now),
        )
        self.conn.commit()

    @staticmethod
    def new_session_id() -> str:
        return datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:4]

    def set_title(self, session_id: str, title: str) -> None:
        self.conn.execute(
            "UPDATE sessions SET title = ?, updated_at = ? WHERE id = ? AND title = ''",
            (title[:60], datetime.now().isoformat(timespec="seconds"), session_id),
        )
        self.conn.commit()

    # --------------------------------------------------------------- messages
    def add_message(self, session_id: str, role: str, content: str) -> None:
        if role not in {"user", "assistant"} or not content.strip():
            return
        now = datetime.now().isoformat(timespec="seconds")
        self.conn.execute(
            "INSERT INTO messages (session_id, role, content, created_at) VALUES (?, ?, ?, ?)",
            (session_id, role, content, now),
        )
        self.conn.execute("UPDATE sessions SET updated_at = ? WHERE id = ?", (now, session_id))
        self.conn.commit()

    def recent_messages(self, session_id: str, limit: int = 30) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT role, content FROM messages WHERE session_id = ? ORDER BY id DESC LIMIT ?",
            (session_id, limit),
        ).fetchall()
        return [{"role": role, "content": content} for role, content in reversed(rows)]

    def last_session(self) -> tuple[str, str] | None:
        row = self.conn.execute(
            "SELECT id, title FROM sessions ORDER BY updated_at DESC LIMIT 1"
        ).fetchone()
        return (row[0], row[1]) if row else None

    def sessions(self, limit: int = 10) -> list[tuple[str, str, str]]:
        return list(
            self.conn.execute(
                "SELECT id, title, updated_at FROM sessions ORDER BY updated_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        )

    def stats(self) -> tuple[int, int]:
        sessions = self.conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        messages = self.conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        return int(sessions), int(messages)

    def close(self) -> None:
        self.conn.close()
