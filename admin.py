"""Admin dashboard, broadcast, analytics, logs.

Mostly unchanged from v1 — small tweaks:
 - Uses the async ticket layer where appropriate
 - Keeps the broadcast rate-limited (Telegram's 30/sec hard cap)
 - Cache invalidation hooks fire automatically via tickets module
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import RetryAfter, TelegramError
from telegram.ext import ContextTypes

import tickets
from database import (
    connect,
    fetch_logs,
    format_ticket_id,
    get_setting,
    log_event,
    parse_ticket_id,
    set_setting,
)

logger = logging.getLogger(__name__)

BOOTSTRAP_ADMIN_USERNAMES = {"iampranjal09", "radheshyam001"}

BROADCAST_BATCH_SIZE = 25
BROADCAST_DELAY_SEC = 0.05
PROGRESS_EVERY = 25

_pending_broadcast: dict[int, str] = {}


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


def is_admin_user_id(user_id: int) -> bool:
    with connect() as con:
        row = con.execute(
            "SELECT is_admin FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()
    return bool(row and row["is_admin"])


def maybe_bootstrap_admin(user_id: int, username: Optional[str]) -> None:
    if not username:
        return
    if username.lower() in BOOTSTRAP_ADMIN_USERNAMES:
        with connect() as con:
            con.execute(
                "UPDATE users SET is_admin = 1 WHERE user_id = ?", (user_id,)
            )
        log_event(
            "INFO", "ADMIN",
            f"Bootstrap: granted admin to @{username}",
            actor_id=user_id,
        )


def get_admin_ids() -> list[int]:
    with connect() as con:
        return [r["user_id"] for r in con.execute(
            "SELECT user_id FROM users WHERE is_admin = 1"
        )]


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

DASHBOARD_TEXT = (
    "🛡 *Cricway Admin Console*\n"
    "_Enterprise Support Dashboard_\n"
    "━━━━━━━━━━━━━━━━━━━━━━\n\n"
    "Select a module below."
)


def dashboard_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📩 Support Tickets", callback_data="adm_tickets"),
            InlineKeyboardButton("👥 Users Database", callback_data="adm_users"),
        ],
        [
            InlineKeyboardButton("📊 Analytics", callback_data="adm_analytics"),
            InlineKeyboardButton("📢 Broadcast", callback_data="adm_broadcast"),
        ],
        [
            InlineKeyboardButton("🤖 AI Settings", callback_data="adm_ai"),
            InlineKeyboardButton("⚙️ System Controls", callback_data="adm_system"),
        ],
        [InlineKeyboardButton("🧾 Logs Viewer", callback_data="adm_logs")],
    ])


def back_to_dashboard_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("⬅️ Back to Dashboard", callback_data="adm_home")]]
    )


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or not is_admin_user_id(user.id):
        await update.message.reply_text("⛔ Access denied.")
        log_event("WARN", "ADMIN", "Non-admin attempted /admin",
                  actor_id=user.id if user else None)
        return
    await update.message.reply_text(
        DASHBOARD_TEXT,
        reply_markup=dashboard_keyboard(),
        parse_mode=ParseMode.MARKDOWN,
    )


async def reply_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/reply CRIC-1001 your message text"""
    user = update.effective_user
    if not user or not is_admin_user_id(user.id):
        await update.message.reply_text("⛔ Access denied.")
        return
    parts = (update.message.text or "").split(maxsplit=2)
    if len(parts) < 3:
        await update.message.reply_text(
            "Usage: `/reply CRIC-1001 your reply text`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    ticket_num = parse_ticket_id(parts[1])
    if ticket_num is None:
        await update.message.reply_text(
            "Invalid ticket id. Example: `CRIC-1001`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    ticket = tickets.get_ticket(ticket_num)
    if not ticket:
        await update.message.reply_text("Ticket not found.")
        return
    body = parts[2].strip()
    tickets.add_reply(ticket_num, user.id, "ADMIN", body)
    tickets.assign_admin(ticket_num, user.id)
    tickets.set_handled_by(ticket_num, "ADMIN")
    try:
        await context.bot.send_message(
            chat_id=ticket["user_id"],
            text=f"💬 *Reply on ticket {format_ticket_id(ticket_num)}*\n\n{body}",
            parse_mode=ParseMode.MARKDOWN,
        )
        await update.message.reply_text(
            f"✅ Reply sent on {format_ticket_id(ticket_num)}.",
            parse_mode=ParseMode.MARKDOWN,
        )
        log_event("INFO", "ADMIN",
                  f"Replied to {format_ticket_id(ticket_num)}", actor_id=user.id)
    except TelegramError as exc:
        await update.message.reply_text(f"⚠️ Could not deliver: {exc}")


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or not is_admin_user_id(user.id):
        await update.message.reply_text("⛔ Access denied.")
        return
    parts = (update.message.text or "").split()
    if len(parts) != 3:
        await update.message.reply_text(
            "Usage: `/status CRIC-1001 OPEN|IN_PROGRESS|RESOLVED`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    ticket_num = parse_ticket_id(parts[1])
    new_status = parts[2].upper()
    if ticket_num is None:
        await update.message.reply_text("Invalid ticket id.")
        return
    if not tickets.update_status(ticket_num, new_status, actor_id=user.id):
        await update.message.reply_text("Update failed (invalid status or unknown ticket).")
        return
    await update.message.reply_text(
        f"✅ {format_ticket_id(ticket_num)} → *{new_status}*",
        parse_mode=ParseMode.MARKDOWN,
    )


async def priority_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or not is_admin_user_id(user.id):
        await update.message.reply_text("⛔ Access denied.")
        return
    parts = (update.message.text or "").split()
    if len(parts) != 3:
        await update.message.reply_text(
            "Usage: `/priority CRIC-1001 LOW|MEDIUM|HIGH`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    ticket_num = parse_ticket_id(parts[1])
    new_priority = parts[2].upper()
    if ticket_num is None:
        await update.message.reply_text("Invalid ticket id.")
        return
    if not tickets.update_priority(ticket_num, new_priority, actor_id=user.id):
        await update.message.reply_text("Update failed (invalid priority or unknown ticket).")
        return
    await update.message.reply_text(
        f"✅ {format_ticket_id(ticket_num)} priority → *{new_priority}*",
        parse_mode=ParseMode.MARKDOWN,
    )


async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or not is_admin_user_id(user.id):
        await update.message.reply_text("⛔ Access denied.")
        return
    text = (update.message.text or "").split(" ", 1)
    if len(text) < 2 or not text[1].strip():
        await update.message.reply_text(
            "Usage: `/broadcast <message>`", parse_mode=ParseMode.MARKDOWN
        )
        return
    message = text[1].strip()
    _pending_broadcast[user.id] = message
    preview = message if len(message) <= 300 else message[:300] + "…"
    await update.message.reply_text(
        f"📢 *Confirm broadcast*\n\n_Preview:_\n{preview}\n\n"
        f"This will be sent to *all* users.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Send", callback_data="bcast_confirm"),
            InlineKeyboardButton("❌ Cancel", callback_data="bcast_cancel"),
        ]]),
    )


# ---------------------------------------------------------------------------
# Inline callbacks
# ---------------------------------------------------------------------------


async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user = update.effective_user
    if not user or not is_admin_user_id(user.id):
        await query.answer("Access denied", show_alert=True)
        return
    await query.answer()
    data = query.data or ""

    if data == "adm_home":
        await query.edit_message_text(
            DASHBOARD_TEXT, reply_markup=dashboard_keyboard(),
            parse_mode=ParseMode.MARKDOWN,
        )
    elif data == "adm_tickets":
        await query.edit_message_text(
            _render_tickets_panel(), reply_markup=back_to_dashboard_kb(),
            parse_mode=ParseMode.MARKDOWN,
        )
    elif data == "adm_users":
        await query.edit_message_text(
            _render_users_panel(), reply_markup=back_to_dashboard_kb(),
            parse_mode=ParseMode.MARKDOWN,
        )
    elif data == "adm_analytics":
        await query.edit_message_text(
            _render_analytics(), reply_markup=back_to_dashboard_kb(),
            parse_mode=ParseMode.MARKDOWN,
        )
    elif data == "adm_broadcast":
        await query.edit_message_text(
            "📢 *Broadcast Center*\n\n"
            "Send `/broadcast <message>` from your chat to compose.\n"
            "You'll get a confirm prompt before delivery.\n\n"
            "Rate limit: ~20 msgs/sec, batched.",
            reply_markup=back_to_dashboard_kb(),
            parse_mode=ParseMode.MARKDOWN,
        )
    elif data == "adm_ai":
        await query.edit_message_text(
            _render_ai_panel(), reply_markup=_ai_keyboard(),
            parse_mode=ParseMode.MARKDOWN,
        )
    elif data == "adm_ai_toggle":
        new_val = "0" if get_setting("ai_enabled", "1") == "1" else "1"
        set_setting("ai_enabled", new_val)
        log_event("INFO", "ADMIN",
                  f"AI {'enabled' if new_val == '1' else 'disabled'}",
                  actor_id=user.id)
        await query.edit_message_text(
            _render_ai_panel(), reply_markup=_ai_keyboard(),
            parse_mode=ParseMode.MARKDOWN,
        )
    elif data == "adm_system":
        await query.edit_message_text(
            _render_system_panel(), reply_markup=_system_keyboard(),
            parse_mode=ParseMode.MARKDOWN,
        )
    elif data == "adm_system_autoclose":
        closed = tickets.auto_close_stale(days=7)
        await query.edit_message_text(
            f"⚙️ *System Controls*\n\nAuto-close run: {closed} ticket(s) closed.\n\n"
            + _render_system_panel(),
            reply_markup=_system_keyboard(),
            parse_mode=ParseMode.MARKDOWN,
        )
    elif data == "adm_logs":
        await query.edit_message_text(
            _render_logs(), reply_markup=back_to_dashboard_kb(),
            parse_mode=ParseMode.MARKDOWN,
        )


async def broadcast_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user = update.effective_user
    if not user or not is_admin_user_id(user.id):
        await query.answer("Access denied", show_alert=True)
        return
    await query.answer()
    data = query.data or ""

    if data == "bcast_cancel":
        _pending_broadcast.pop(user.id, None)
        await query.edit_message_text("❌ Broadcast cancelled.")
        return

    if data == "bcast_confirm":
        message = _pending_broadcast.pop(user.id, None)
        if not message:
            await query.edit_message_text("⚠️ No pending broadcast.")
            return
        # Run as a true background task — do NOT block the callback handler
        asyncio.create_task(_run_broadcast(query, context, user.id, message))
        await query.edit_message_text(
            "📢 *Broadcast started…* You'll see live progress here.",
            parse_mode=ParseMode.MARKDOWN,
        )


# ---------------------------------------------------------------------------
# Broadcast engine
# ---------------------------------------------------------------------------


async def _run_broadcast(query, context, admin_id: int, message: str) -> None:
    with connect() as con:
        user_ids = [r["user_id"] for r in con.execute("SELECT user_id FROM users")]

    total = len(user_ids)
    sent = 0
    failed = 0
    log_event("INFO", "BROADCAST",
              f"Broadcast started → {total} users", actor_id=admin_id)

    progress_msg = query.message  # reuse message reference

    for idx, uid in enumerate(user_ids, start=1):
        try:
            await context.bot.send_message(
                chat_id=uid,
                text=f"📢 *Cricway Announcement*\n\n{message}",
                parse_mode=ParseMode.MARKDOWN,
            )
            sent += 1
        except RetryAfter as exc:
            await asyncio.sleep(float(exc.retry_after) + 0.5)
            try:
                await context.bot.send_message(
                    chat_id=uid,
                    text=f"📢 *Cricway Announcement*\n\n{message}",
                    parse_mode=ParseMode.MARKDOWN,
                )
                sent += 1
            except TelegramError:
                failed += 1
        except TelegramError:
            failed += 1
        await asyncio.sleep(BROADCAST_DELAY_SEC)

        if idx % PROGRESS_EVERY == 0 or idx == total:
            try:
                await progress_msg.edit_text(
                    f"📢 *Broadcasting…*\n\nProgress: {idx} / {total}\n"
                    f"✅ Sent: {sent}   ⚠️ Failed: {failed}",
                    parse_mode=ParseMode.MARKDOWN,
                )
            except TelegramError:
                pass

    log_event("INFO", "BROADCAST",
              f"Broadcast finished — sent={sent} failed={failed} total={total}",
              actor_id=admin_id)
    try:
        await progress_msg.edit_text(
            f"✅ *Broadcast complete*\n\nTotal: {total}\n"
            f"Delivered: {sent}\nFailed: {failed}",
            parse_mode=ParseMode.MARKDOWN,
        )
    except TelegramError:
        pass


# ---------------------------------------------------------------------------
# Panel renderers
# ---------------------------------------------------------------------------


def _render_tickets_panel() -> str:
    open_tk = tickets.list_tickets(status="OPEN", limit=10)
    in_prog = tickets.list_tickets(status="IN_PROGRESS", limit=5)
    lines = ["📩 *Support Tickets*", "━━━━━━━━━━━━━━━━━━━━━━", ""]
    lines.append("*Open queue:*")
    if not open_tk:
        lines.append("_No open tickets._")
    else:
        lines.extend(_ticket_line(t) for t in open_tk)
    lines.append("")
    lines.append("*In progress:*")
    if not in_prog:
        lines.append("_None._")
    else:
        lines.extend(_ticket_line(t) for t in in_prog)
    lines.append("")
    lines.append("Reply: `/reply CRIC-XXXX message`")
    lines.append("Status: `/status CRIC-XXXX RESOLVED`")
    lines.append("Priority: `/priority CRIC-XXXX HIGH`")
    return "\n".join(lines)


def _ticket_line(t: dict) -> str:
    ts = (t.get("updated_at") or "")[:19].replace("T", " ")
    subj = (t.get("subject") or "").replace("\n", " ")
    if len(subj) > 70:
        subj = subj[:70] + "…"
    return (
        f"{tickets.STATUS_EMOJI.get(t['status'], '•')} "
        f"{tickets.PRIORITY_EMOJI.get(t['priority'], '')} "
        f"`{format_ticket_id(t['ticket_id'])}` "
        f"u:`{t['user_id']}` — {subj}\n   _{ts}_"
    )


def _render_users_panel() -> str:
    with connect() as con:
        total = con.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]
        admins = con.execute("SELECT COUNT(*) AS c FROM users WHERE is_admin = 1").fetchone()["c"]
        recent = list(
            con.execute(
                "SELECT user_id, username, total_requests, last_active "
                "FROM users ORDER BY last_active DESC LIMIT 10"
            )
        )
    lines = ["👥 *Users Database*", "━━━━━━━━━━━━━━━━━━━━━━", ""]
    lines.append(f"Total users: *{total}*")
    lines.append(f"Admins: *{admins}*")
    lines.append("")
    lines.append("*Most recent activity:*")
    if not recent:
        lines.append("_No users yet._")
    else:
        for r in recent:
            uname = r["username"] or "—"
            ts = (r["last_active"] or "")[:19].replace("T", " ")
            lines.append(
                f"• `{r['user_id']}` @{uname} — {r['total_requests']} req — _{ts}_"
            )
    return "\n".join(lines)


def _render_analytics() -> str:
    stats = tickets.ticket_stats()
    with connect() as con:
        total_users = con.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
        active_24h = con.execute(
            "SELECT COUNT(*) AS c FROM users WHERE last_active >= ?", (cutoff,)
        ).fetchone()["c"]
        peak_row = con.execute(
            "SELECT strftime('%H', created_at) AS hr, COUNT(*) AS c "
            "FROM ticket_replies WHERE sender_role = 'USER' "
            "GROUP BY hr ORDER BY c DESC LIMIT 1"
        ).fetchone()
    peak = f"{peak_row['hr']}:00 UTC ({peak_row['c']} msgs)" if peak_row else "—"

    handled_total = stats["ai_handled"] + stats["admin_handled"]
    if handled_total:
        ai_ratio = stats["ai_handled"] / handled_total * 100
        admin_ratio = stats["admin_handled"] / handled_total * 100
        ratio = f"{ai_ratio:.0f}% AI / {admin_ratio:.0f}% Admin"
    else:
        ratio = "—"

    return (
        "📊 *Analytics Dashboard*\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"👥 *Total users:* {total_users}\n"
        f"🟢 *Active (24h):* {active_24h}\n\n"
        f"🎫 *Tickets total:* {stats['total']}\n"
        f"   • Open: {stats['open']}\n"
        f"   • In progress: {stats['in_progress']}\n"
        f"   • Resolved: {stats['resolved']}\n\n"
        f"🤖 *Handled split:* {ratio}\n"
        f"⏰ *Peak activity:* {peak}"
    )


def _render_ai_panel() -> str:
    enabled = get_setting("ai_enabled", "1") == "1"
    model = get_setting("ai_model", "gemini-2.5-flash")
    return (
        "🤖 *AI Settings*\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"Status: {'🟢 Enabled' if enabled else '🔴 Disabled'}\n"
        f"Model: `{model}`\n\n"
        "Tap below to toggle the assistant on/off."
    )


def _ai_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔁 Toggle AI", callback_data="adm_ai_toggle")],
        [InlineKeyboardButton("⬅️ Back to Dashboard", callback_data="adm_home")],
    ])


def _render_system_panel() -> str:
    return (
        "⚙️ *System Controls*\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "• Auto-close stale tickets (>= 7 days inactive).\n"
        "• Bot version: enterprise v2.0 (cached + async)\n"
        "• Storage: SQLite (cricway.db) + WAL\n"
    )


def _system_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🧹 Run Auto-Close", callback_data="adm_system_autoclose")],
        [InlineKeyboardButton("⬅️ Back to Dashboard", callback_data="adm_home")],
    ])


def _render_logs() -> str:
    rows = fetch_logs(limit=25)
    lines = ["🧾 *Logs Viewer*  (latest 25)", "━━━━━━━━━━━━━━━━━━━━━━", ""]
    if not rows:
        lines.append("_No logs yet._")
    else:
        for r in rows:
            ts = (r["created_at"] or "")[:19].replace("T", " ")
            actor = f"u:{r['actor_id']}" if r["actor_id"] else "—"
            lvl = r["level"]
            cat = r["category"]
            msg = (r["message"] or "").replace("`", "'")
            if len(msg) > 90:
                msg = msg[:90] + "…"
            lines.append(f"`{ts}` [{lvl}] {cat} {actor} — {msg}")
    return "\n".join(lines)
