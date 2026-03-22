"""
Session memory — SQLite (local) or PostgreSQL (cloud) backed persistence.
Set DATABASE_URL env var to use PostgreSQL, e.g. on Render.
Falls back to SQLite when DATABASE_URL is not set.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Optional

DB_PATH     = Path(__file__).parent.parent / "data" / "sessions.db"
DATABASE_URL = os.environ.get("DATABASE_URL", "")
IS_PG       = bool(DATABASE_URL)

# Try to import psycopg2 — fall back to SQLite if not available
if IS_PG:
    try:
        import psycopg2                         # noqa: F401
        from psycopg2.extras import RealDictCursor  # noqa: F401
        _PG_AVAILABLE = True
    except ImportError:
        IS_PG = False
        _PG_AVAILABLE = False
else:
    _PG_AVAILABLE = False


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
    user_pk = "SERIAL PRIMARY KEY" if IS_PG else "INTEGER PRIMARY KEY AUTOINCREMENT"
    # Create all tables in one transaction
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
                username       TEXT DEFAULT ''
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
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS users (
                id            {user_pk},
                username      TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                salt          TEXT NOT NULL,
                role          TEXT DEFAULT 'member',
                token         TEXT,
                created_at    TEXT
            )
        """)
    # Migrate existing DBs — separate connection so failure doesn't roll back table creation
    try:
        with _conn() as cur:
            cur.execute("ALTER TABLE sessions ADD COLUMN username TEXT DEFAULT ''")
    except Exception:
        pass  # Column already exists — ignore


# ── Session CRUD ─────────────────────────────────────────────────────────────

def create_session(name: str = "", username: str = "") -> str:
    sid = str(uuid.uuid4())[:8]
    now = datetime.now().isoformat()
    with _conn() as cur:
        cur.execute(
            "INSERT INTO sessions (id, name, phase, created_at, updated_at, username) VALUES (?,?,?,?,?,?)",
            (sid, name or f"Project {sid}", "greeting", now, now, username),
        )
    return sid


def get_session(session_id: str) -> Optional[dict]:
    with _conn() as cur:
        return cur.execute(
            "SELECT * FROM sessions WHERE id=?", (session_id,)
        ).fetchone()


def list_sessions(username: str = "") -> list[dict]:
    with _conn() as cur:
        if username:
            return cur.execute(
                "SELECT * FROM sessions WHERE username=? ORDER BY updated_at DESC",
                (username,),
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


# ── User / Auth ───────────────────────────────────────────────────────────────

def _hash_password(password: str, salt: str) -> str:
    return hashlib.sha256((salt + password).encode()).hexdigest()


def upsert_admin(username: str, password: str) -> None:
    """Create admin user or reset password if already exists (used for seeding)."""
    salt = secrets.token_hex(16)
    password_hash = _hash_password(password, salt)
    token = secrets.token_hex(32)
    now = datetime.now().isoformat()
    with _conn() as cur:
        existing = cur.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()
        if existing:
            cur.execute(
                "UPDATE users SET password_hash=?, salt=?, token=?, role=? WHERE username=?",
                (password_hash, salt, token, "admin", username)
            )
        else:
            cur.execute(
                "INSERT INTO users (username, password_hash, salt, role, token, created_at) VALUES (?,?,?,?,?,?)",
                (username, password_hash, salt, "admin", token, now)
            )


def create_user(username: str, password: str, role: str = "member") -> Optional[dict]:
    """Returns created user dict or None if username taken."""
    salt = secrets.token_hex(16)
    password_hash = _hash_password(password, salt)
    token = secrets.token_hex(32)
    now = datetime.now().isoformat()
    try:
        with _conn() as cur:
            cur.execute(
                "INSERT INTO users (username, password_hash, salt, role, token, created_at) VALUES (?,?,?,?,?,?)",
                (username, password_hash, salt, role, token, now)
            )
        return get_user_by_token(token)
    except Exception:
        return None  # username already taken


def get_user_by_username(username: str) -> Optional[dict]:
    with _conn() as cur:
        return cur.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()


def get_user_by_token(token: str) -> Optional[dict]:
    if not token:
        return None
    with _conn() as cur:
        return cur.execute("SELECT * FROM users WHERE token=?", (token,)).fetchone()


def verify_login(username: str, password: str) -> Optional[dict]:
    """Returns user dict with fresh token if credentials valid, else None."""
    user = get_user_by_username(username)
    if not user:
        return None
    expected = _hash_password(password, user["salt"])
    if expected != user["password_hash"]:
        return None
    # Rotate token on login
    new_token = secrets.token_hex(32)
    with _conn() as cur:
        cur.execute("UPDATE users SET token=? WHERE username=?", (new_token, username))
    user["token"] = new_token
    return user


def list_users() -> list[dict]:
    with _conn() as cur:
        return cur.execute("SELECT id, username, role, created_at FROM users ORDER BY created_at").fetchall()


def update_user_role(username: str, role: str) -> None:
    with _conn() as cur:
        cur.execute("UPDATE users SET role=? WHERE username=?", (role, username))


def delete_user(username: str) -> None:
    with _conn() as cur:
        cur.execute("DELETE FROM users WHERE username=?", (username,))


def user_count() -> int:
    with _conn() as cur:
        row = cur.execute("SELECT COUNT(*) as cnt FROM users").fetchone()
        return row["cnt"] if row else 0
