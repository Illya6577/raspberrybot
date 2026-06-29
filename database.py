"""
SQLite database layer for the Telegram call bot.
"""

import sqlite3
import json
import os
from datetime import datetime, date, timedelta
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
                chat_id    INTEGER NOT NULL,
                user_id    INTEGER NOT NULL,
                first_name TEXT NOT NULL DEFAULT '',
                emoji      TEXT NOT NULL DEFAULT '👤',
                active     INTEGER NOT NULL DEFAULT 1,
                PRIMARY KEY (chat_id, user_id)
            );

            CREATE TABLE IF NOT EXISTS chat_settings (
                chat_id       INTEGER PRIMARY KEY,
                settings_json TEXT NOT NULL DEFAULT '{}'
            );

            CREATE TABLE IF NOT EXISTS hidden_users (
                scope   TEXT PRIMARY KEY,
                user_id INTEGER
            );

            CREATE TABLE IF NOT EXISTS message_log (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id    INTEGER NOT NULL,
                user_id    INTEGER NOT NULL,
                first_name TEXT NOT NULL DEFAULT '',
                sent_at    TEXT NOT NULL  -- ISO-8601 UTC: "2024-03-15 14:22:05"
            );

            CREATE INDEX IF NOT EXISTS idx_msglog_chat_user
                ON message_log(chat_id, user_id);
            CREATE INDEX IF NOT EXISTS idx_msglog_sent_at
                ON message_log(sent_at);
        """)


# ─────────────── members ───────────────

def ensure_member(chat_id: int, user_id: int, first_name: str):
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


# ─────────────── message log ───────────────

def log_message(chat_id: int, user_id: int, first_name: str, sent_at: Optional[datetime] = None):
    ts = (sent_at or datetime.utcnow()).strftime("%Y-%m-%d %H:%M:%S")
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO message_log (chat_id, user_id, first_name, sent_at) VALUES (?,?,?,?)",
            (chat_id, user_id, first_name, ts)
        )


def get_user_stats(chat_id: int, user_id: int) -> dict:
    """Return message counts per period + first message date for a user in a chat."""
    now = datetime.utcnow()
    today     = now.date().isoformat()
    week_ago  = (now.date() - timedelta(days=7)).isoformat()
    month_ago = (now.date() - timedelta(days=30)).isoformat()
    year_ago  = (now.date() - timedelta(days=365)).isoformat()

    with get_conn() as conn:
        def count(since: Optional[str] = None) -> int:
            if since:
                row = conn.execute(
                    "SELECT COUNT(*) as n FROM message_log WHERE chat_id=? AND user_id=? AND sent_at>=?",
                    (chat_id, user_id, since)
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT COUNT(*) as n FROM message_log WHERE chat_id=? AND user_id=?",
                    (chat_id, user_id)
                ).fetchone()
            return row["n"] if row else 0

        first_row = conn.execute(
            "SELECT MIN(sent_at) as first FROM message_log WHERE chat_id=? AND user_id=?",
            (chat_id, user_id)
        ).fetchone()
        first_msg = first_row["first"] if first_row else None

    return {
        "day":   count(today),
        "week":  count(week_ago),
        "month": count(month_ago),
        "year":  count(year_ago),
        "all":   count(),
        "first": first_msg,
    }


def get_top_users(chat_id: int, limit: int = 10) -> list[tuple]:
    """Return [(user_id, first_name, count)] sorted by total messages desc."""
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT user_id, first_name, COUNT(*) as n
            FROM message_log
            WHERE chat_id=?
            GROUP BY user_id
            ORDER BY n DESC
            LIMIT ?
        """, (chat_id, limit)).fetchall()
    return [(r["user_id"], r["first_name"], r["n"]) for r in rows]


def get_today_top_users(chat_id: int, limit: int = 10) -> list[tuple]:
    """Return top users by messages sent today."""
    today = datetime.utcnow().date().isoformat()
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT user_id, first_name, COUNT(*) as n
            FROM message_log
            WHERE chat_id=? AND sent_at>=?
            GROUP BY user_id
            ORDER BY n DESC
            LIMIT ?
        """, (chat_id, today, limit)).fetchall()
    return [(r["user_id"], r["first_name"], r["n"]) for r in rows]


def get_week_top_users(chat_id: int, limit: int = 10) -> list[tuple]:
    """Return top users by messages sent in the last 7 days."""
    week_ago = (datetime.utcnow().date() - timedelta(days=7)).isoformat()
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT user_id, first_name, COUNT(*) as n
            FROM message_log
            WHERE chat_id=? AND sent_at>=?
            GROUP BY user_id
            ORDER BY n DESC
            LIMIT ?
        """, (chat_id, week_ago, limit)).fetchall()
    return [(r["user_id"], r["first_name"], r["n"]) for r in rows]


def get_week_messages(chat_id: int) -> int:
    week_ago = (datetime.utcnow().date() - timedelta(days=7)).isoformat()
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) as n FROM message_log WHERE chat_id=? AND sent_at>=?",
            (chat_id, week_ago)
        ).fetchone()
    return row["n"] if row else 0


def get_total_messages(chat_id: int) -> int:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) as n FROM message_log WHERE chat_id=?", (chat_id,)
        ).fetchone()
    return row["n"] if row else 0


def get_today_messages(chat_id: int) -> int:
    today = datetime.utcnow().date().isoformat()
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) as n FROM message_log WHERE chat_id=? AND sent_at>=?",
            (chat_id, today)
        ).fetchone()
    return row["n"] if row else 0


# ─────────────── chat export import ───────────────

def import_from_telegram_export(chat_id: int, export_data: dict) -> dict:
    """
    Parse a Telegram Desktop JSON export (result.json) and populate:
      - members table (from unique senders)
      - message_log table (one row per text message)

    Returns {"messages": n, "users": n} counts of imported rows.
    """
    messages = export_data.get("messages", [])
    imported_msgs = 0
    users_seen: dict[int, str] = {}  # user_id -> first_name

    rows_to_insert = []

    for msg in messages:
        # Only real messages (not service events)
        if msg.get("type") != "message":
            continue

        from_id_raw = msg.get("from_id", "")
        # Telegram export uses "user123456" format for from_id
        if isinstance(from_id_raw, str) and from_id_raw.startswith("user"):
            try:
                uid = int(from_id_raw[4:])
            except ValueError:
                continue
        elif isinstance(from_id_raw, int):
            uid = from_id_raw
        else:
            continue

        first_name = str(msg.get("from", "")).strip() or "Невідомий"
        users_seen[uid] = first_name

        # Parse date — Telegram export uses "2024-03-15T14:22:05" format
        date_str = msg.get("date", "")
        try:
            sent_at = datetime.fromisoformat(date_str)
        except (ValueError, TypeError):
            sent_at = datetime.utcnow()

        ts = sent_at.strftime("%Y-%m-%d %H:%M:%S")
        rows_to_insert.append((chat_id, uid, first_name, ts))

    with get_conn() as conn:
        # Insert members
        for uid, fname in users_seen.items():
            conn.execute("""
                INSERT INTO members (chat_id, user_id, first_name)
                VALUES (?, ?, ?)
                ON CONFLICT(chat_id, user_id) DO UPDATE SET first_name=excluded.first_name
            """, (chat_id, uid, fname))

        # Bulk insert messages
        conn.executemany(
            "INSERT OR IGNORE INTO message_log (chat_id, user_id, first_name, sent_at) VALUES (?,?,?,?)",
            rows_to_insert
        )
        imported_msgs = len(rows_to_insert)

    return {"messages": imported_msgs, "users": len(users_seen)}


# ─────────────── global stats (super admin) ───────────────

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


def count_total_logged_messages() -> int:
    with get_conn() as conn:
        row = conn.execute("SELECT COUNT(*) as n FROM message_log").fetchone()
    return row["n"] if row else 0
