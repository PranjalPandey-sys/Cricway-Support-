"""SQLite persistence — single shared connection + WAL + async-friendly.

Changes vs. v1:
 - Single long-lived connection (check_same_thread=False) instead of open/close
   per query. SQLite + WAL handles concurrent reads safely.
 - Aggressive PRAGMAs (cache_size=-64MB, synchronous=NORMAL, mmap_size=128MB).
 - `log_event` writes go through a fire-and-forget background queue so logging
   never sits on the request path.
 - `arun()` helper offloads any blocking DB call to a thread executor so it
   does not freeze the asyncio event loop.
"""
from __future__ import annotations

import asyncio
import logging
import os
import queue
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterator, Optional

logger = logging.getLogger(__name__)

DB_PATH = Path(os.environ.get("CRICWAY_DB", "cricway.db"))

TICKET_PREFIX = "CRIC"
TICKET_START = 1001

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id        INTEGER PRIMARY KEY,
    username       TEXT,
    first_name     TEXT,
    first_seen     TEXT NOT NULL,
    last_active    TEXT NOT NULL,
    total_requests INTEGER NOT NULL DEFAULT 0,
    is_admin       INTEGER NOT NULL DEFAULT 0,
    language       TEXT NOT NULL DEFAULT 'en'
);

CREATE TABLE IF NOT EXISTS tickets (
    ticket_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id        INTEGER NOT NULL,
    subject        TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'OPEN',
    priority       TEXT NOT NULL DEFAULT 'MEDIUM',
    handled_by     TEXT NOT NULL DEFAULT 'PENDING',
    assigned_admin INTEGER,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ticket_replies (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id   INTEGER NOT NULL,
    sender_id   INTEGER,
    sender_role TEXT NOT NULL,
    message     TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    FOREIGN KEY (ticket_id) REFERENCES tickets(ticket_id)
);

CREATE TABLE IF NOT EXISTS logs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    level      TEXT NOT NULL,
    category   TEXT NOT NULL,
    actor_id   INTEGER,
    message    TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE INDEX IF NOT EXISTS idx_tickets_status  ON tickets(status);
CREATE INDEX IF NOT EXISTS idx_tickets_user    ON tickets(user_id);
CREATE INDEX IF NOT EXISTS idx_tickets_updated ON tickets(updated_at);
CREATE INDEX IF NOT EXISTS idx_replies_ticket  ON ticket_replies(ticket_id);
CREATE INDEX IF NOT EXISTS idx_logs_created    ON logs(created_at);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Single shared connection
# ---------------------------------------------------------------------------

_CON: Optional[sqlite3.Connection] = None
_CON_LOCK = threading.RLock()


def _open_connection() -> sqlite3.Connection:
    con = sqlite3.connect(
        DB_PATH,
        isolation_level=None,           # autocommit
        timeout=10,
        check_same_thread=False,        # safe: we serialize via _CON_LOCK
    )
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("PRAGMA journal_mode = WAL")
    con.execute("PRAGMA synchronous = NORMAL")
    con.execute("PRAGMA temp_store = MEMORY")
    con.execute("PRAGMA cache_size = -65536")     # 64MB page cache
    con.execute("PRAGMA mmap_size = 134217728")   # 128MB memory-mapped I/O
    con.execute("PRAGMA busy_timeout = 5000")
    return con


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    """Re-entrant access to the shared connection. Use for ALL SQL."""
    global _CON
    with _CON_LOCK:
        if _CON is None:
            _CON = _open_connection()
        yield _CON


async def arun(fn: Callable[[], Any]) -> Any:
    """Offload a blocking DB call to the default thread pool."""
    return await asyncio.get_running_loop().run_in_executor(None, fn)


# ---------------------------------------------------------------------------
# Async fire-and-forget log writer
# ---------------------------------------------------------------------------

_log_q: "queue.Queue[tuple[str, str, Optional[int], str, str]]" = queue.Queue(maxsize=10_000)
_log_thread: Optional[threading.Thread] = None


def _log_worker() -> None:
    while True:
        item = _log_q.get()
        if item is None:        # shutdown sentinel
            return
        level, category, actor_id, msg, ts = item
        try:
            with connect() as con:
                con.execute(
                    "INSERT INTO logs (level, category, actor_id, message, created_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (level, category, actor_id, msg, ts),
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("log_event drop: %s", exc)
        finally:
            _log_q.task_done()


def _ensure_log_thread() -> None:
    global _log_thread
    if _log_thread is None or not _log_thread.is_alive():
        _log_thread = threading.Thread(target=_log_worker, daemon=True, name="log-writer")
        _log_thread.start()


def log_event(level: str, category: str, message: str, actor_id: Optional[int] = None) -> None:
    """Non-blocking — pushes into a queue, written by background thread."""
    _ensure_log_thread()
    try:
        _log_q.put_nowait((level, category, actor_id, message, now_iso()))
    except queue.Full:
        logger.warning("log queue full — dropping %s/%s", level, category)


def fetch_logs(limit: int = 30) -> list[sqlite3.Row]:
    with connect() as con:
        return list(
            con.execute(
                "SELECT level, category, actor_id, message, created_at "
                "FROM logs ORDER BY id DESC LIMIT ?",
                (limit,),
            )
        )


# ---------------------------------------------------------------------------
# Init + settings
# ---------------------------------------------------------------------------


def init_db() -> None:
    with connect() as con:
        con.executescript(SCHEMA)
        existing_cols = {row["name"] for row in con.execute("PRAGMA table_info(users)")}
        if "first_name" not in existing_cols:
            con.execute("ALTER TABLE users ADD COLUMN first_name TEXT")
        cur = con.execute("SELECT seq FROM sqlite_sequence WHERE name='tickets'")
        if cur.fetchone() is None:
            con.execute(
                "INSERT INTO sqlite_sequence (name, seq) VALUES ('tickets', ?)",
                (TICKET_START - 1,),
            )
        con.execute(
            "INSERT OR IGNORE INTO settings (key, value) VALUES ('ai_enabled', '1')"
        )
        con.execute(
            "INSERT OR IGNORE INTO settings (key, value) VALUES ('ai_model', ?)",
            (os.environ.get("AI_MODEL", "gemini-2.5-flash"),),
        )
    _ensure_log_thread()


# Settings cache (1s TTL — survives bursts but stays fresh)
_settings_cache: dict[str, tuple[float, Optional[str]]] = {}
_SETTINGS_TTL = 1.0


def get_setting(key: str, default: Optional[str] = None) -> Optional[str]:
    import time as _t
    cached = _settings_cache.get(key)
    if cached and cached[0] > _t.monotonic():
        return cached[1] if cached[1] is not None else default
    with connect() as con:
        row = con.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        value = row["value"] if row else None
    _settings_cache[key] = (_t.monotonic() + _SETTINGS_TTL, value)
    return value if value is not None else default


def set_setting(key: str, value: str) -> None:
    with connect() as con:
        con.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
    _settings_cache.pop(key, None)


# ---------------------------------------------------------------------------
# Ticket id helpers
# ---------------------------------------------------------------------------


def format_ticket_id(numeric: int) -> str:
    return f"{TICKET_PREFIX}-{numeric}"


def parse_ticket_id(value: str) -> Optional[int]:
    if not value:
        return None
    s = value.strip().upper()
    if s.startswith(f"{TICKET_PREFIX}-"):
        s = s[len(TICKET_PREFIX) + 1 :]
    return int(s) if s.isdigit() else None
