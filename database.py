"""
SQLite database layer for the Telegram call bot.
"""

import sqlite3
import json
import os
from typing import Optional

DB_PATH = os.environ.get("DB_PATH", "bot.db")


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS members (
                chat_id   INTEGER NOT NULL,
                user_id   INTEGER NOT NULL,
                first_name TEXT NOT NULL DEFAULT '',
                emoji     TEXT NOT NULL DEFAULT '👤',
                active    INTEGER NOT NULL DEFAULT 1,
                PRIMARY KEY (chat_id, user_id)
            );

            CREATE TABLE IF NOT EXISTS chat_settings (
                chat_id      INTEGER PRIMARY KEY,
                settings_json TEXT NOT NULL DEFAULT '{}'
            );

            CREATE TABLE IF NOT EXISTS hidden_users (
                scope    TEXT PRIMARY KEY,
                user_id  INTEGER
            );
        """)


# ─────────────── members ───────────────

def ensure_member(chat_id: int, user_id: int, first_name: str):
    """Insert member if not exists; update first_name."""
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO members (chat_id, user_id, first_name)
            VALUES (?, ?, ?)
            ON CONFLICT(chat_id, user_id) DO UPDATE SET first_name=excluded.first_name
        """, (chat_id, user_id, first_name))


def get_registered_members(chat_id: int) -> list:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT user_id, first_name, emoji, active FROM members WHERE chat_id=?",
            (chat_id,)
        ).fetchall()
    return [(r["user_id"], r["first_name"], r["emoji"], bool(r["active"])) for r in rows]


def get_member(chat_id: int, user_id: int) -> Optional[tuple]:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT user_id, first_name, emoji, active FROM members WHERE chat_id=? AND user_id=?",
            (chat_id, user_id)
        ).fetchone()
    if row is None:
        return None
    return (row["user_id"], row["first_name"], row["emoji"], bool(row["active"]))


def set_active(chat_id: int, user_id: int, active: bool):
    with get_conn() as conn:
        conn.execute(
            "UPDATE members SET active=? WHERE chat_id=? AND user_id=?",
            (1 if active else 0, chat_id, user_id)
        )


def set_active_all_chats(user_id: int, active: bool) -> int:
    """Set active status across all chats. Returns number of chats affected."""
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE members SET active=? WHERE user_id=?",
            (1 if active else 0, user_id)
        )
        return cur.rowcount


def get_emoji(chat_id: int, user_id: int) -> Optional[str]:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT emoji FROM members WHERE chat_id=? AND user_id=?",
            (chat_id, user_id)
        ).fetchone()
    return row["emoji"] if row else None


def set_emoji(chat_id: int, user_id: int, emoji: str):
    with get_conn() as conn:
        conn.execute(
            "UPDATE members SET emoji=? WHERE chat_id=? AND user_id=?",
            (emoji, chat_id, user_id)
        )


# ─────────────── chat settings ───────────────

def get_chat_settings(chat_id: int) -> dict:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT settings_json FROM chat_settings WHERE chat_id=?",
            (chat_id,)
        ).fetchone()
    if row is None:
        return {}
    return json.loads(row["settings_json"])


def set_chat_setting(chat_id: int, key: str, value):
    settings = get_chat_settings(chat_id)
    settings[key] = value
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO chat_settings (chat_id, settings_json)
            VALUES (?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET settings_json=excluded.settings_json
        """, (chat_id, json.dumps(settings)))


# ─────────────── hidden users ───────────────

def _scope_key(chat_id: Optional[int]) -> str:
    return f"chat_{chat_id}" if chat_id else "global"


def get_hidden_user(chat_id: Optional[int]) -> Optional[int]:
    key = _scope_key(chat_id)
    with get_conn() as conn:
        row = conn.execute(
            "SELECT user_id FROM hidden_users WHERE scope=?", (key,)
        ).fetchone()
    return row["user_id"] if row else None


def set_hidden_user(chat_id: Optional[int], user_id: Optional[int]):
    key = _scope_key(chat_id)
    with get_conn() as conn:
        if user_id is None:
            conn.execute("DELETE FROM hidden_users WHERE scope=?", (key,))
        else:
            conn.execute("""
                INSERT INTO hidden_users (scope, user_id) VALUES (?, ?)
                ON CONFLICT(scope) DO UPDATE SET user_id=excluded.user_id
            """, (key, user_id))


# ─────────────── stats ───────────────

def count_chats() -> int:
    with get_conn() as conn:
        row = conn.execute("SELECT COUNT(DISTINCT chat_id) as n FROM members").fetchone()
    return row["n"] if row else 0


def count_members() -> int:
    with get_conn() as conn:
        row = conn.execute("SELECT COUNT(*) as n FROM members").fetchone()
    return row["n"] if row else 0


def count_active_members() -> int:
    with get_conn() as conn:
        row = conn.execute("SELECT COUNT(*) as n FROM members WHERE active=1").fetchone()
    return row["n"] if row else 0
