"""Ticket lifecycle — fast create + cached reads + background writes."""
from __future__ import annotations

import asyncio
from typing import Optional

from cache import TICKETS_LIST_CACHE
from database import arun, connect, format_ticket_id, log_event, now_iso

VALID_STATUSES = {"OPEN", "IN_PROGRESS", "RESOLVED", "CLOSED"}
VALID_PRIORITIES = {"LOW", "MEDIUM", "HIGH"}
VALID_HANDLERS = {"AI", "ADMIN", "PENDING", "SYSTEM"}

PRIORITY_EMOJI = {"LOW": "🟢", "MEDIUM": "🟡", "HIGH": "🔴"}
STATUS_EMOJI = {"OPEN": "🟠", "IN_PROGRESS": "🔵", "RESOLVED": "✅", "CLOSED": "⚪"}


# ---------------------------------------------------------------------------
# Create — must stay synchronous & fast: caller awaits the returned id
# ---------------------------------------------------------------------------


def create_ticket(user_id: int, subject: str, priority: str = "MEDIUM") -> int:
    if priority not in VALID_PRIORITIES:
        priority = "MEDIUM"
    ts = now_iso()
    subject = subject.strip()[:1000]
    with connect() as con:
        cur = con.execute(
            "INSERT INTO tickets (user_id, subject, status, priority, handled_by, "
            "created_at, updated_at) VALUES (?, ?, 'OPEN', ?, 'PENDING', ?, ?)",
            (user_id, subject, priority, ts, ts),
        )
        ticket_id = int(cur.lastrowid)
        con.execute(
            "INSERT INTO ticket_replies (ticket_id, sender_id, sender_role, message, "
            "created_at) VALUES (?, ?, 'USER', ?, ?)",
            (ticket_id, user_id, subject, ts),
        )
    TICKETS_LIST_CACHE.invalidate(f"u:{user_id}")
    log_event("INFO", "USER", f"Ticket {format_ticket_id(ticket_id)} created", actor_id=user_id)
    return ticket_id


async def acreate_ticket(user_id: int, subject: str, priority: str = "MEDIUM") -> int:
    """Async wrapper — never blocks the event loop."""
    return await arun(lambda: create_ticket(user_id, subject, priority))


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def get_ticket(ticket_id: int) -> Optional[dict]:
    with connect() as con:
        row = con.execute(
            "SELECT * FROM tickets WHERE ticket_id = ?", (ticket_id,)
        ).fetchone()
        return dict(row) if row else None


def list_tickets(
    status: Optional[str] = None,
    user_id: Optional[int] = None,
    limit: int = 20,
) -> list[dict]:
    # Cache only for the common case: per-user list, no status filter
    cache_key = None
    if status is None and user_id is not None and limit <= 20:
        cache_key = f"u:{user_id}"
        cached = TICKETS_LIST_CACHE.get(cache_key)
        if cached is not None:
            return cached

    sql = "SELECT * FROM tickets"
    params: list = []
    where: list[str] = []
    if status:
        where.append("status = ?")
        params.append(status)
    if user_id is not None:
        where.append("user_id = ?")
        params.append(user_id)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY updated_at DESC LIMIT ?"
    params.append(limit)
    with connect() as con:
        result = [dict(r) for r in con.execute(sql, params)]
    if cache_key:
        TICKETS_LIST_CACHE.set(cache_key, result)
    return result


def list_replies(ticket_id: int, limit: int = 20) -> list[dict]:
    with connect() as con:
        return [
            dict(r)
            for r in con.execute(
                "SELECT * FROM ticket_replies WHERE ticket_id = ? "
                "ORDER BY id ASC LIMIT ?",
                (ticket_id, limit),
            )
        ]


# ---------------------------------------------------------------------------
# Writes — sync versions kept; async wrappers for handlers
# ---------------------------------------------------------------------------


def add_reply(ticket_id: int, sender_id: Optional[int], sender_role: str, message: str) -> None:
    ts = now_iso()
    with connect() as con:
        con.execute(
            "INSERT INTO ticket_replies (ticket_id, sender_id, sender_role, message, "
            "created_at) VALUES (?, ?, ?, ?, ?)",
            (ticket_id, sender_id, sender_role, message[:4000], ts),
        )
        row = con.execute(
            "SELECT user_id FROM tickets WHERE ticket_id = ?", (ticket_id,)
        ).fetchone()
        con.execute(
            "UPDATE tickets SET updated_at = ? WHERE ticket_id = ?", (ts, ticket_id)
        )
    if row:
        TICKETS_LIST_CACHE.invalidate(f"u:{row['user_id']}")


def update_status(ticket_id: int, status: str, actor_id: Optional[int] = None) -> bool:
    if status not in VALID_STATUSES:
        return False
    ts = now_iso()
    with connect() as con:
        cur = con.execute(
            "UPDATE tickets SET status = ?, updated_at = ? WHERE ticket_id = ?",
            (status, ts, ticket_id),
        )
        changed = cur.rowcount > 0
        if changed:
            row = con.execute(
                "SELECT user_id FROM tickets WHERE ticket_id = ?", (ticket_id,)
            ).fetchone()
            if row:
                TICKETS_LIST_CACHE.invalidate(f"u:{row['user_id']}")
    if changed:
        log_event(
            "INFO", "ADMIN",
            f"Ticket {format_ticket_id(ticket_id)} → {status}",
            actor_id=actor_id,
        )
    return changed


def update_priority(ticket_id: int, priority: str, actor_id: Optional[int] = None) -> bool:
    if priority not in VALID_PRIORITIES:
        return False
    ts = now_iso()
    with connect() as con:
        cur = con.execute(
            "UPDATE tickets SET priority = ?, updated_at = ? WHERE ticket_id = ?",
            (priority, ts, ticket_id),
        )
        changed = cur.rowcount > 0
    if changed:
        log_event(
            "INFO", "ADMIN",
            f"Ticket {format_ticket_id(ticket_id)} priority → {priority}",
            actor_id=actor_id,
        )
    return changed


def set_handled_by(ticket_id: int, handled_by: str) -> None:
    if handled_by not in VALID_HANDLERS:
        return
    with connect() as con:
        con.execute(
            "UPDATE tickets SET handled_by = ?, updated_at = ? WHERE ticket_id = ?",
            (handled_by, now_iso(), ticket_id),
        )


def assign_admin(ticket_id: int, admin_id: int) -> None:
    with connect() as con:
        con.execute(
            "UPDATE tickets SET assigned_admin = ?, status = 'IN_PROGRESS', "
            "updated_at = ? WHERE ticket_id = ?",
            (admin_id, now_iso(), ticket_id),
        )


def auto_close_stale(days: int = 7) -> int:
    cutoff = now_iso()
    with connect() as con:
        cur = con.execute(
            "UPDATE tickets SET status = 'RESOLVED', handled_by = 'SYSTEM', "
            "updated_at = ? WHERE status IN ('OPEN', 'IN_PROGRESS') "
            "AND julianday(?) - julianday(updated_at) >= ?",
            (cutoff, cutoff, days),
        )
        closed = cur.rowcount
    if closed:
        TICKETS_LIST_CACHE.clear()
        log_event(
            "INFO", "SYSTEM",
            f"Auto-closed {closed} stale ticket(s) (>= {days} days inactive)",
        )
    return closed


def ticket_stats() -> dict:
    with connect() as con:
        total = con.execute("SELECT COUNT(*) AS c FROM tickets").fetchone()["c"]
        by_status = {
            row["status"]: row["c"]
            for row in con.execute(
                "SELECT status, COUNT(*) AS c FROM tickets GROUP BY status"
            )
        }
        by_handler = {
            row["handled_by"]: row["c"]
            for row in con.execute(
                "SELECT handled_by, COUNT(*) AS c FROM tickets GROUP BY handled_by"
            )
        }
    return {
        "total": total,
        "open": by_status.get("OPEN", 0),
        "in_progress": by_status.get("IN_PROGRESS", 0),
        "resolved": by_status.get("RESOLVED", 0),
        "ai_handled": by_handler.get("AI", 0),
        "admin_handled": by_handler.get("ADMIN", 0),
        "pending": by_handler.get("PENDING", 0),
    }


# ---------------------------------------------------------------------------
# Background fire-and-forget helpers
# ---------------------------------------------------------------------------


def schedule(coro_or_fn, *args, **kwargs) -> None:
    """Run a sync function as a background asyncio task without awaiting it."""
    loop = asyncio.get_running_loop()
    if asyncio.iscoroutinefunction(coro_or_fn):
        loop.create_task(coro_or_fn(*args, **kwargs))
    else:
        loop.run_in_executor(None, lambda: coro_or_fn(*args, **kwargs))
