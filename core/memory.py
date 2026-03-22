"""
Session memory — SQLite-backed persistence.
Stores sessions, conversation messages, requirements, task plans.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

DB_PATH = Path(__file__).parent.parent / "data" / "sessions.db"


def _conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with _conn() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS sessions (
            id          TEXT PRIMARY KEY,
            name        TEXT,
            phase       TEXT DEFAULT 'greeting',
            created_at  TEXT,
            updated_at  TEXT,
            requirements TEXT,
            task_plan   TEXT,
            project_folder TEXT,
            tech_stack  TEXT
        );

        CREATE TABLE IF NOT EXISTS messages (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id  TEXT,
            role        TEXT,
            content     TEXT,
            timestamp   TEXT,
            metadata    TEXT
        );
        """)


# ─── Session CRUD ────────────────────────────────────────

def create_session(name: str = "") -> str:
    sid = str(uuid.uuid4())[:8]
    now = datetime.now().isoformat()
    with _conn() as conn:
        conn.execute(
            "INSERT INTO sessions (id, name, phase, created_at, updated_at) VALUES (?,?,?,?,?)",
            (sid, name or f"Project {sid}", "greeting", now, now),
        )
    return sid


def get_session(session_id: str) -> Optional[dict]:
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM sessions WHERE id=?", (session_id,)
        ).fetchone()
    return dict(row) if row else None


def list_sessions() -> list[dict]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM sessions ORDER BY updated_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def delete_session(session_id: str) -> None:
    with _conn() as conn:
        conn.execute("DELETE FROM messages WHERE session_id=?", (session_id,))
        conn.execute("DELETE FROM sessions WHERE id=?", (session_id,))


def update_session(session_id: str, **kwargs) -> None:
    kwargs["updated_at"] = datetime.now().isoformat()
    for key, val in kwargs.items():
        if isinstance(val, (dict, list)):
            kwargs[key] = json.dumps(val)
    sets = ", ".join(f"{k}=?" for k in kwargs)
    vals = list(kwargs.values()) + [session_id]
    with _conn() as conn:
        conn.execute(f"UPDATE sessions SET {sets} WHERE id=?", vals)


# ─── Message CRUD ────────────────────────────────────────

def add_message(session_id: str, role: str, content: str, metadata: dict = None) -> None:
    with _conn() as conn:
        conn.execute(
            "INSERT INTO messages (session_id, role, content, timestamp, metadata) VALUES (?,?,?,?,?)",
            (
                session_id,
                role,
                content,
                datetime.now().isoformat(),
                json.dumps(metadata or {}),
            ),
        )


def get_messages(session_id: str) -> list[dict]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM messages WHERE session_id=? ORDER BY id ASC",
            (session_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_conversation_history(session_id: str) -> list[dict]:
    """Returns messages in Claude API format (role + content only)."""
    msgs = get_messages(session_id)
    return [{"role": m["role"], "content": m["content"]} for m in msgs]
