"""
Session memory — SQLite (local) or PostgreSQL (cloud) backed persistence.
Set DATABASE_URL env var to use PostgreSQL, e.g. on Render.
Falls back to SQLite when DATABASE_URL is not set.
"""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Optional

DB_PATH     = Path(__file__).parent.parent / "data" / "sessions.db"
DATABASE_URL = os.environ.get("DATABASE_URL", "")
IS_PG       = bool(DATABASE_URL)


# ── Thin cursor wrapper ──────────────────────────────────────────────────────

class _Cur:
    """Normalises SQLite Row and psycopg2 RealDictRow to plain dicts."""

    def __init__(self, raw):
        self._c = raw

    def execute(self, sql: str, params=()):
        if IS_PG:
            sql = sql.replace("?", "%s")
        self._c.execute(sql, params)
        return self

    def fetchone(self) -> Optional[dict]:
        row = self._c.fetchone()
        return dict(row) if row else None

    def fetchall(self) -> list[dict]:
        return [dict(r) for r in (self._c.fetchall() or [])]


@contextmanager
def _conn():
    """Yield a normalised _Cur inside a committed transaction."""
    if IS_PG:
        import psycopg2                         # type: ignore
        from psycopg2.extras import RealDictCursor  # type: ignore
        conn = psycopg2.connect(DATABASE_URL)
        try:
            raw = conn.cursor(cursor_factory=RealDictCursor)
            yield _Cur(raw)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    else:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(DB_PATH))
        conn.row_factory = sqlite3.Row
        try:
            raw = conn.cursor()
            yield _Cur(raw)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


# ── Schema ───────────────────────────────────────────────────────────────────

def init_db() -> None:
    msg_pk = "SERIAL PRIMARY KEY" if IS_PG else "INTEGER PRIMARY KEY AUTOINCREMENT"
    with _conn() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                id             TEXT PRIMARY KEY,
                name           TEXT,
                phase          TEXT DEFAULT 'greeting',
                created_at     TEXT,
                updated_at     TEXT,
                requirements   TEXT,
                task_plan      TEXT,
                project_folder TEXT,
                tech_stack     TEXT,
                user           TEXT DEFAULT ''
            )
        """)
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS messages (
                id         {msg_pk},
                session_id TEXT,
                role       TEXT,
                content    TEXT,
                timestamp  TEXT,
                metadata   TEXT
            )
        """)
        # Migrate existing DBs that don't have the user column yet
        try:
            cur.execute("ALTER TABLE sessions ADD COLUMN user TEXT DEFAULT ''")
        except Exception:
            pass  # Column already exists — ignore


# ── Session CRUD ─────────────────────────────────────────────────────────────

def create_session(name: str = "", user: str = "") -> str:
    sid = str(uuid.uuid4())[:8]
    now = datetime.now().isoformat()
    with _conn() as cur:
        cur.execute(
            "INSERT INTO sessions (id, name, phase, created_at, updated_at, user) VALUES (?,?,?,?,?,?)",
            (sid, name or f"Project {sid}", "greeting", now, now, user),
        )
    return sid


def get_session(session_id: str) -> Optional[dict]:
    with _conn() as cur:
        return cur.execute(
            "SELECT * FROM sessions WHERE id=?", (session_id,)
        ).fetchone()


def list_sessions(user: str = "") -> list[dict]:
    with _conn() as cur:
        if user:
            return cur.execute(
                "SELECT * FROM sessions WHERE user=? ORDER BY updated_at DESC",
                (user,),
            ).fetchall()
        return cur.execute(
            "SELECT * FROM sessions ORDER BY updated_at DESC"
        ).fetchall()


def delete_session(session_id: str) -> None:
    with _conn() as cur:
        cur.execute("DELETE FROM messages WHERE session_id=?", (session_id,))
        cur.execute("DELETE FROM sessions WHERE id=?", (session_id,))


def update_session(session_id: str, **kwargs) -> None:
    kwargs["updated_at"] = datetime.now().isoformat()
    for key, val in list(kwargs.items()):
        if isinstance(val, (dict, list)):
            kwargs[key] = json.dumps(val)
    sets = ", ".join(f"{k}=?" for k in kwargs)
    vals = list(kwargs.values()) + [session_id]
    with _conn() as cur:
        cur.execute(f"UPDATE sessions SET {sets} WHERE id=?", vals)


# ── Message CRUD ─────────────────────────────────────────────────────────────

def add_message(session_id: str, role: str, content: str, metadata: dict = None) -> None:
    with _conn() as cur:
        cur.execute(
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
    with _conn() as cur:
        return cur.execute(
            "SELECT * FROM messages WHERE session_id=? ORDER BY id ASC",
            (session_id,),
        ).fetchall()


def get_conversation_history(session_id: str) -> list[dict]:
    """Returns messages in Claude API format (role + content only)."""
    return [{"role": m["role"], "content": m["content"]} for m in get_messages(session_id)]
