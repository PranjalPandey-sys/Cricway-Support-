"""Cricway Enterprise Support Bot — production-stable entry point.

Stability improvements in this version:
 ─────────────────────────────────────────────────────────────────────────
 • Flask health server replaces bare HTTPServer — handles GET / HEAD / POST
   correctly so UptimeRobot and Render never see 501 / 502.
 • Flask runs in its own daemon thread; Telegram polling runs in main thread.
 • run_polling() is wrapped in a while-True restart loop with exponential
   back-off — a single TimedOut or network blip NEVER kills the bot.
 • telegram.error.TimedOut is caught globally and does NOT increment the
   restart counter (it is normal polling noise, not a real crash).
 • Application is rebuilt fresh on every restart loop iteration so there
   are no stale event-loop or handler references.
 • Image uploads use file_id caching (already in cache.py / ui.py) so
   large photos are uploaded once and reused forever after.
 • Health endpoint binds the port BEFORE DB init so Render does not kill
   the process during a slow cold-start.
 • _BOT_READY flag lets /ready return 503 during init and 200 once live.
"""
from __future__ import annotations

import asyncio
import logging
import os
import threading
import time as _time
from datetime import datetime, time, timezone
from typing import Optional

# ── Flask health server ────────────────────────────────────────────────────
from flask import Flask

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import NetworkError, TelegramError, TimedOut
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import admin
import tickets
from ai import AI_FALLBACK_TEXT, aget_ai_response, faq_match, faq_suggest
from cache import (
    STATIC_SCREENS,
    STATUS_CACHE,
    register_static,
)
from database import arun, connect, format_ticket_id, init_db, log_event, now_iso
from ui import card, md, show_screen

# ── Logging ────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ── Startup diagnostics ────────────────────────────────────────────────────
print("🚀 BOT STARTING (production-stable)…")
print("TOKEN    FOUND:", bool(os.getenv("TOKEN")))
print("GEMINI   FOUND:", bool(os.getenv("GEMINI_API_KEY")))

# ── Required environment variables ────────────────────────────────────────
TOKEN = os.getenv("TOKEN")
if not TOKEN:
    raise ValueError("❌ TOKEN is missing — set it in Render environment variables.")

SPAM_REPEAT_LIMIT = 3
STATUS_CACHE_KEY  = "status_screen"

# Signals to the /ready endpoint that the bot has fully initialised.
_BOT_READY = threading.Event()


# ===========================================================================
# SECTION 1 — FLASK HEALTH SERVER
# Runs in a daemon thread. Handles GET, HEAD, and POST so Render health
# checks and UptimeRobot monitors never see a 501 / 502.
# ===========================================================================

flask_app = Flask(__name__)


@flask_app.route("/", methods=["GET", "HEAD", "POST"])
def health_root():
    """Root endpoint — always returns 200. Render and UptimeRobot probe here."""
    return "OK — Cricway Bot is running", 200


@flask_app.route("/ready", methods=["GET", "HEAD"])
def health_ready():
    """Strict readiness gate — 503 while initialising, 200 once polling starts."""
    if _BOT_READY.is_set():
        return "READY", 200
    return "STARTING", 503


def _run_flask() -> None:
    """Start Flask on the PORT Render assigns (default 10000)."""
    port = int(os.getenv("PORT", "10000"))
    print(f"🌐 Flask health server starting on port {port}…")
    # use_reloader=False is critical — reloader forks the process and creates
    # a second bot instance which triggers the Telegram Conflict error.
    flask_app.run(
        host="0.0.0.0",
        port=port,
        use_reloader=False,
        threaded=True,          # handle concurrent health probes
    )


# ===========================================================================
# SECTION 2 — KEYBOARDS
# Pure functions — cheap to build. Static keyboards are reused across calls.
# ===========================================================================


def home_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🆘 Support Center", callback_data="usr_support"),
            InlineKeyboardButton("🤖 AI Assistant",   callback_data="usr_ai"),
        ],
        [
            InlineKeyboardButton("🎫 My Tickets",  callback_data="usr_tickets"),
            InlineKeyboardButton("📊 Live Status", callback_data="usr_status"),
        ],
        [
            InlineKeyboardButton("📚 Help Center", callback_data="usr_faq"),
            InlineKeyboardButton("⚠️ Safety Info", callback_data="usr_safety"),
        ],
    ])


def back_home_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("🏠 Home", callback_data="usr_home")]]
    )


def status_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Refresh", callback_data="usr_status_refresh")],
        [InlineKeyboardButton("🏠 Home",    callback_data="usr_home")],
    ])


def support_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎫 My Tickets", callback_data="usr_tickets")],
        [InlineKeyboardButton("📚 Browse FAQ", callback_data="usr_faq")],
        [InlineKeyboardButton("🏠 Home",       callback_data="usr_home")],
    ])


def ai_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🆘 Talk to Human", callback_data="usr_support")],
        [InlineKeyboardButton("🏠 Home",          callback_data="usr_home")],
    ])


def tickets_list_keyboard(tlist: list[dict]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for t in tlist[:6]:
        rows.append([
            InlineKeyboardButton(
                f"{tickets.STATUS_EMOJI.get(t['status'], '•')} "
                f"{format_ticket_id(t['ticket_id'])}",
                callback_data=f"tkt_view_{t['ticket_id']}",
            )
        ])
    rows.append([InlineKeyboardButton("🏠 Home", callback_data="usr_home")])
    return InlineKeyboardMarkup(rows)


def ticket_view_keyboard(ticket_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Refresh",     callback_data=f"tkt_view_{ticket_id}")],
        [InlineKeyboardButton("🎫 All Tickets", callback_data="usr_tickets")],
        [InlineKeyboardButton("🏠 Home",        callback_data="usr_home")],
    ])


def ai_followup_keyboard(ticket_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(
            "💬 Not satisfied? Talk to a human",
            callback_data=f"tkt_escalate_{ticket_id}",
        )],
        [InlineKeyboardButton(
            f"🎫 View ticket {format_ticket_id(ticket_id)}",
            callback_data=f"tkt_view_{ticket_id}",
        )],
        [InlineKeyboardButton("🏠 Home", callback_data="usr_home")],
    ])


def escalated_keyboard(ticket_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(
            f"🎫 View ticket {format_ticket_id(ticket_id)}",
            callback_data=f"tkt_view_{ticket_id}",
        )],
        [InlineKeyboardButton("🏠 Home", callback_data="usr_home")],
    ])


# ===========================================================================
# SECTION 3 — DATABASE HELPERS
# Thin wrappers; all SQL goes through the shared connection in database.py.
# ===========================================================================


def upsert_user(
    user_id: int, username: Optional[str], first_name: Optional[str]
) -> None:
    ts = now_iso()
    with connect() as con:
        con.execute(
            "INSERT INTO users (user_id, username, first_name, first_seen, "
            "last_active, total_requests) VALUES (?, ?, ?, ?, ?, 0) "
            "ON CONFLICT(user_id) DO UPDATE SET "
            "username = excluded.username, first_name = excluded.first_name, "
            "last_active = excluded.last_active",
            (user_id, username, first_name, ts, ts),
        )


def increment_request_count(user_id: int) -> None:
    with connect() as con:
        con.execute(
            "UPDATE users SET total_requests = total_requests + 1, last_active = ? "
            "WHERE user_id = ?",
            (now_iso(), user_id),
        )


def fetch_user(user_id: int) -> Optional[dict]:
    with connect() as con:
        row = con.execute(
            "SELECT user_id, username, first_name, first_seen, total_requests "
            "FROM users WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        return dict(row) if row else None


def display_name(user) -> str:
    if not user:
        return "there"
    return user.first_name or user.username or "there"


def short_subject(text: str, n: int = 60) -> str:
    text = (text or "").replace("\n", " ").strip()
    return text if len(text) <= n else text[: n - 1] + "…"


def fmt_ts(ts: str) -> str:
    if not ts:
        return "—"
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return dt.strftime("%d %b %Y, %H:%M UTC")
    except ValueError:
        return ts[:19].replace("T", " ")


# ===========================================================================
# SECTION 4 — STATIC SCREEN BUILDERS
# Called ONCE at startup; results stored in STATIC_SCREENS dict (cache.py).
# Every button tap for static pages reads from memory — no rebuild, no DB.
# ===========================================================================


def _build_support_static() -> str:
    body = (
        "Hi there — describe your issue in a single message and we'll open a tracked ticket instantly.\n\n"
        "*How it works*\n"
        "1️⃣  *Describe your issue* — share details in your own words.\n"
        "2️⃣  *Get an instant ticket* — we generate a `CRIC-XXXX` id.\n"
        "3️⃣  *Expert team review* — AI first, then a human if needed.\n"
        "4️⃣  *Real-time updates* — replies arrive right here."
    )
    return card("Support Center", "We're here to help, 24/7", body,
                "🟢 General · 🟡 Urgent · 🔴 Critical",
                "💡 *Tip:* be clear and include any IDs / details — it speeds things up.")


def _build_ai_static() -> str:
    body = (
        "Hello — I'm Cricway AI, your always-on assistant.\n\n"
        "Type your question below and I'll reply in seconds. If I can't fully resolve it, "
        "I'll loop in a human teammate automatically.\n\n"
        "*I can help with:*\n"
        "• Account & login\n"
        "• Payments & withdrawals\n"
        "• Features & usage\n"
        "• Technical issues\n"
        "• General questions"
    )
    return card("Cricway AI Assistant", "Smart. Fast. Always here.", body,
                "🧠 Smart · ⚡ Fast · 🔒 Private",
                "💡 _Not satisfied with the answer? Tap Talk to a human anytime._")


def _build_safety_static() -> str:
    body = (
        "🔒 *Protect your account*\n"
        "Never share your login, OTP, or personal info.\n\n"
        "🔗 *Avoid unofficial links*\n"
        "Don't click suspicious links or DMs from unknown sources.\n\n"
        "🎧 *Only trust official support*\n"
        "We will *never* ask for your password or payment details.\n\n"
        "🚩 *Report suspicious activity*\n"
        "Help us keep the community safe — flag anything that seems off."
    )
    return card("Your Safety, Our Priority", "Play safe. Stay safe. Always.", body,
                "🛡 Encrypted · 👁 24/7 monitoring",
                "🔐 _Your security. Our commitment._")


def _build_faq_static() -> str:
    body = (
        "*Q1 · Getting started*\n"
        "Sign up, verify, and explore. New users may receive an onboarding bonus.\n\n"
        "*Q2 · Getting support*\n"
        "Just describe your issue — every message becomes a tracked ticket.\n\n"
        "*Q3 · Talking to a human*\n"
        "If our AI can't resolve it, your case is auto-escalated to a human agent.\n\n"
        "*Q4 · Deposits & withdrawals*\n"
        "Deposits 2–5 min · Withdrawals 15–30 min on average.\n\n"
        "*Q5 · Response times*\n"
        "AI: instant · Human: typically 5–30 min."
    )
    return card("Help Center", "Answers at a glance", body,
                "📚 Search · 🆘 Open ticket · 🤖 Ask AI",
                "💡 _Not finding an answer? Just type your question._")


def _prebuild_static_screens() -> None:
    register_static("support", _build_support_static())
    register_static("ai",      _build_ai_static())
    register_static("safety",  _build_safety_static())
    register_static("faq",     _build_faq_static())
    logger.info("✅ Static screens prebuilt and cached.")


# ===========================================================================
# SECTION 5 — DYNAMIC SCREEN BUILDERS
# These depend on live data (user row, ticket stats) so they can't be cached
# at startup, but they are still cheap.
# ===========================================================================


def build_home_screen(user, user_row: Optional[dict]) -> str:
    name = display_name(user)
    is_returning = bool(user_row and user_row.get("total_requests", 0) > 0)
    greeting = (
        f"Welcome back, *{md(name)}* 👋"
        if is_returning
        else f"Welcome aboard, *{md(name)}* 👋"
    )
    body = (
        f"{greeting}\n\n"
        "We're here to make every match smooth — pick an option below to get started."
    )
    return card("Cricway Support", "Enterprise Support · 24/7", body,
                "🆘 Support · 🤖 AI · 🎫 Tickets\n📊 Status · 📚 Help · ⚠️ Safety",
                "💡 _One team. One goal. Your satisfaction._")


def build_status_screen() -> str:
    # Cached for 30 s — avoids DB hit on every rapid Refresh tap.
    cached = STATUS_CACHE.get(STATUS_CACHE_KEY)
    if cached is not None:
        return cached

    stats      = tickets.ticket_stats()
    open_total = stats["open"] + stats["in_progress"]
    if open_total >= 25:
        state, label = "🔴", "Issue Detected"
    elif open_total >= 10:
        state, label = "🟡", "High Load"
    else:
        state, label = "🟢", "Operational"

    body = (
        f"🟢 *Platform* — Operational\n"
        f"🟢 *Payments* — Operational\n"
        f"{state} *Support Desk* — {label}\n"
        f"🟢 *API Services* — Operational\n"
        f"🟢 *Security* — Operational\n\n"
        f"*Snapshot*\n"
        f"• Open tickets: `{stats['open']}`\n"
        f"• In progress: `{stats['in_progress']}`\n"
        f"• Resolved (all-time): `{stats['resolved']}`"
    )
    rendered = card(
        "Live System Status", "Real-time · Reliable · Always on", body,
        f"🔄 Last updated: {datetime.now(timezone.utc).strftime('%H:%M UTC')}",
        "💡 _Pull to refresh — get the latest status instantly._",
    )
    STATUS_CACHE.set(STATUS_CACHE_KEY, rendered)
    return rendered


def build_my_tickets_screen(user_id: int, tlist: list[dict]) -> str:
    if not tlist:
        return card("My Tickets", "Your support history",
                    "📭 *No active tickets — you're all set!*\n\n"
                    "When you send us a message, it'll appear here as a tracked case "
                    "you can follow end-to-end.",
                    "🆘 Open Support · 🤖 Ask AI",
                    "💡 _Pro tip: include details for the fastest reply._")

    lines = [
        f"{tickets.STATUS_EMOJI.get(t['status'], '•')} "
        f"{tickets.PRIORITY_EMOJI.get(t['priority'], '')} "
        f"`{format_ticket_id(t['ticket_id'])}` — {md(short_subject(t['subject']))}"
        for t in tlist[:6]
    ]
    return card("My Tickets", "Your support history",
                "\n".join(lines),
                f"Showing {min(len(tlist), 6)} of {len(tlist)} ticket(s)",
                "💡 _Tap any ticket to view its timeline._")


def build_ticket_detail_screen(ticket: dict, replies: list[dict]) -> str:
    pretty   = format_ticket_id(ticket["ticket_id"])
    status   = ticket["status"]
    priority = ticket["priority"]
    handled  = ticket.get("handled_by") or "PENDING"

    timeline_lines = []
    for r in replies[-6:]:
        ts   = fmt_ts(r["created_at"])
        role = r["sender_role"]
        icon = {"USER": "🙋", "ADMIN": "👨‍💼", "AI": "🤖", "SYSTEM": "⚙️"}.get(role, "•")
        msg  = md(short_subject(r["message"], 90))
        timeline_lines.append(f"{icon} *{role}* · _{ts}_\n   {msg}")
    timeline = "\n\n".join(timeline_lines) if timeline_lines else "_No activity yet._"

    body = (
        f"*Status:* {tickets.STATUS_EMOJI.get(status, '•')} `{status}`\n"
        f"*Priority:* {tickets.PRIORITY_EMOJI.get(priority, '•')} `{priority}`\n"
        f"*Handled by:* `{handled}`\n"
        f"*Created:* _{fmt_ts(ticket['created_at'])}_\n"
        f"*Updated:* _{fmt_ts(ticket['updated_at'])}_\n\n"
        f"*Timeline*\n{timeline}"
    )
    return card(f"Ticket {pretty}", "Case timeline & history", body,
                "🔄 Refresh · 🎫 All tickets",
                "💡 _A reply from our team will land here in real time._")


# ===========================================================================
# SECTION 6 — COMMAND HANDLERS
# ===========================================================================


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user     = update.effective_user
    user_row = None
    if user:
        upsert_user(user.id, user.username, user.first_name)
        admin.maybe_bootstrap_admin(user.id, user.username)
        log_event("INFO", "USER", "/start", actor_id=user.id)
        user_row = fetch_user(user.id)
    context.user_data.clear()
    await show_screen(update, context, image="home.png",
                      text=build_home_screen(user, user_row),
                      keyboard=home_keyboard())


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await show_screen(update, context, image=None,
                      text=STATIC_SCREENS["faq"], keyboard=back_home_keyboard())


async def safety_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await show_screen(update, context, image="safety.png",
                      text=STATIC_SCREENS["safety"], keyboard=back_home_keyboard())


# ===========================================================================
# SECTION 7 — CALLBACK HANDLERS
# ===========================================================================

# Static button → (cache key, image filename, keyboard builder)
_STATIC_ROUTES: dict[str, tuple[str, Optional[str], callable]] = {
    "usr_support": ("support", "support_hub.png", support_keyboard),
    "usr_ai":      ("ai",      "ai.png",          ai_keyboard),
    "usr_safety":  ("safety",  "safety.png",      back_home_keyboard),
    "usr_faq":     ("faq",     None,              back_home_keyboard),
}


async def user_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user  = update.effective_user
    data  = query.data or ""

    # Answer immediately — removes Telegram's loading spinner.
    await query.answer("Refreshed ✅" if data == "usr_status_refresh" else "")

    # ── Static cached pages (instant, no DB) ──────────────────────────────
    if data in _STATIC_ROUTES:
        cache_name, image, kb_fn = _STATIC_ROUTES[data]
        await show_screen(update, context, image=image,
                          text=STATIC_SCREENS[cache_name], keyboard=kb_fn())
        if user:
            asyncio.create_task(
                arun(lambda: upsert_user(user.id, user.username, user.first_name))
            )
        return

    # ── Home (personalised greeting) ──────────────────────────────────────
    if data == "usr_home":
        if user:
            upsert_user(user.id, user.username, user.first_name)
        user_row = fetch_user(user.id) if user else None
        await show_screen(update, context, image="home.png",
                          text=build_home_screen(user, user_row),
                          keyboard=home_keyboard())
        return

    # ── Live status (30-second TTL cache) ─────────────────────────────────
    if data in ("usr_status", "usr_status_refresh"):
        if data == "usr_status_refresh":
            STATUS_CACHE.invalidate(STATUS_CACHE_KEY)
        await show_screen(update, context, image="status.png",
                          text=build_status_screen(), keyboard=status_keyboard())
        return

    # ── My Tickets ────────────────────────────────────────────────────────
    if data == "usr_tickets":
        if user:
            upsert_user(user.id, user.username, user.first_name)
        my = tickets.list_tickets(user_id=user.id, limit=10) if user else []
        await show_screen(update, context, image="support_hub.png",
                          text=build_my_tickets_screen(user.id if user else 0, my),
                          keyboard=tickets_list_keyboard(my))
        return


async def ticket_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user  = update.effective_user
    data  = query.data or ""

    if data.startswith("tkt_view_"):
        await query.answer()
        ticket_id = int(data.split("_")[-1])
        ticket    = tickets.get_ticket(ticket_id)
        if not ticket or (
            user and ticket["user_id"] != user.id
            and not admin.is_admin_user_id(user.id)
        ):
            await query.answer("Ticket not found.", show_alert=True)
            return
        replies = tickets.list_replies(ticket_id, limit=20)
        await show_screen(update, context, image="support_ticket.png",
                          text=build_ticket_detail_screen(ticket, replies),
                          keyboard=ticket_view_keyboard(ticket_id))

    elif data.startswith("tkt_escalate_"):
        await query.answer("Escalated to a human agent ✅", show_alert=False)
        ticket_id = int(data.split("_")[-1])
        ticket    = tickets.get_ticket(ticket_id)
        if not ticket or (user and ticket["user_id"] != user.id):
            return

        async def _do_escalate():
            tickets.set_handled_by(ticket_id, "PENDING")
            tickets.update_status(ticket_id, "OPEN",
                                  actor_id=user.id if user else None)
            tickets.update_priority(ticket_id, "HIGH",
                                    actor_id=user.id if user else None)
            tickets.add_reply(
                ticket_id, user.id if user else None, "SYSTEM",
                "User requested human follow-up (Not satisfied with AI).",
            )
            await _notify_admins_new_ticket(
                context, ticket_id, user, ticket["subject"], escalated=True
            )

        asyncio.create_task(_do_escalate())

        body = (
            f"✅ *Escalated to a human agent.*\n\n"
            f"Ticket `{format_ticket_id(ticket_id)}` is now *HIGH priority* and "
            f"our team has been notified. You'll get a reply right here as soon as "
            f"an agent picks it up."
        )
        await show_screen(
            update, context, image="support_ticket.png",
            text=card("Escalation Confirmed", "We're on it", body,
                      "🟡 Your ticket is now under review",
                      "💡 _You can keep adding details — just send another message._"),
            keyboard=escalated_keyboard(ticket_id),
        )


# ===========================================================================
# SECTION 8 — MESSAGE HANDLER (instant ACK + background AI)
# ===========================================================================


async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    text = (update.message.text or "").strip()
    if not user or not text:
        return

    # ── Spam guard (in-memory, zero DB cost) ──────────────────────────────
    last   = context.user_data.get("last_msg")
    repeat = context.user_data.get("repeat_count", 0)
    repeat = repeat + 1 if last == text else 1
    context.user_data["last_msg"]     = text
    context.user_data["repeat_count"] = repeat
    if repeat >= SPAM_REPEAT_LIMIT:
        await update.message.reply_text(
            "⚠️ Looks like you sent that already — we've logged it. "
            "Add new details and we'll get to you faster."
        )
        log_event("WARN", "USER", f"Spam-repeat ({repeat}x): {text[:80]}",
                  actor_id=user.id)
        return

    # ── User upsert + request count ───────────────────────────────────────
    upsert_user(user.id, user.username, user.first_name)
    admin.maybe_bootstrap_admin(user.id, user.username)
    increment_request_count(user.id)

    # ── Create ticket immediately (~1 ms SQLite insert) ───────────────────
    ticket_id = tickets.create_ticket(user.id, text)
    pretty_id = format_ticket_id(ticket_id)

    # ── Instant FAQ check (pure in-memory keyword index) ──────────────────
    rule = faq_match(text)
    if rule:
        body = (
            f"🎫 *Ticket {pretty_id}* opened — instant match found.\n\n"
            f"{rule['response']}"
        )
        asyncio.create_task(arun(lambda: (
            tickets.add_reply(ticket_id, None, "AI", rule["response"]),
            tickets.set_handled_by(ticket_id, "AI"),
            tickets.update_status(ticket_id, "RESOLVED"),
        )))
        await show_screen(
            update, context, image="support_ticket.png",
            text=card("Instant Answer", "Matched from our knowledge base", body,
                      f"Status: {tickets.STATUS_EMOJI['RESOLVED']} RESOLVED",
                      "💡 _Need a human? Tap Not satisfied below._"),
            keyboard=ai_followup_keyboard(ticket_id),
        )
        return

    # ── Instant ACK — user sees their ticket ID in <300 ms ────────────────
    ack_msg = await update.message.reply_text(
        f"⏳ *Processing your request…* `{pretty_id}`\n"
        f"_We'll update this message with the answer in a few seconds._",
        parse_mode=ParseMode.MARKDOWN,
    )

    # ── Fire AI processing as a background task ────────────────────────────
    # The handler returns NOW. The AI reply edits the ack message when ready.
    asyncio.create_task(
        _process_ai_in_background(
            context=context,
            chat_id=update.effective_chat.id,
            ack_message_id=ack_msg.message_id,
            ticket_id=ticket_id,
            user=user,
            text=text,
        )
    )


async def _process_ai_in_background(
    *,
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    ack_message_id: int,
    ticket_id: int,
    user,
    text: str,
) -> None:
    """AI call — runs detached from the message handler so UI is never blocked."""
    pretty_id = format_ticket_id(ticket_id)

    try:
        ai_reply, escalate = await aget_ai_response(text)
    except Exception as exc:          # noqa: BLE001
        logger.exception("Background AI failed: %s", exc)
        ai_reply, escalate = AI_FALLBACK_TEXT, True

    await arun(lambda: tickets.add_reply(ticket_id, None, "AI", ai_reply))

    if escalate or ai_reply == AI_FALLBACK_TEXT:
        await arun(lambda: tickets.set_handled_by(ticket_id, "PENDING"))
        await arun(lambda: tickets.update_priority(ticket_id, "HIGH"))
        asyncio.create_task(_notify_admins_new_ticket(context, ticket_id, user, text))

        suggestion       = faq_suggest(text)
        suggestion_block = ""
        if suggestion:
            suggestion_block = "\n\n💡 *You might be looking for:*\n" + suggestion["response"]

        body = (
            f"📩 We received your request — *Ticket {pretty_id}* is open.\n\n"
            f"Our AI couldn't resolve this with full confidence, so we've escalated it "
            f"to a human agent. You'll hear back here shortly.{suggestion_block}"
        )
        await _replace_ack_with_screen(
            context, chat_id, ack_message_id,
            image="support_ticket.png",
            text=card("Escalated to Human Support", "A teammate will reply soon", body,
                      f"Status: {tickets.STATUS_EMOJI['OPEN']} OPEN · 🔴 HIGH priority",
                      "💡 _Add more details anytime — just send another message._"),
            keyboard=escalated_keyboard(ticket_id),
        )
        return

    await arun(lambda: tickets.set_handled_by(ticket_id, "AI"))
    await arun(lambda: tickets.update_status(ticket_id, "RESOLVED"))

    body = f"🎫 *Ticket {pretty_id}* — answered by AI.\n\n{ai_reply}"
    await _replace_ack_with_screen(
        context, chat_id, ack_message_id,
        image="ai.png",
        text=card("Cricway AI · Reply", "Smart. Fast. Always here.", body,
                  f"Status: {tickets.STATUS_EMOJI['RESOLVED']} RESOLVED",
                  "💡 _Not satisfied? Tap below to talk to a human._"),
        keyboard=ai_followup_keyboard(ticket_id),
    )


async def _replace_ack_with_screen(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    ack_message_id: int,
    *,
    image: Optional[str],
    text: str,
    keyboard: InlineKeyboardMarkup,
) -> None:
    """Edit the 'processing…' text message, or delete + send as fallback."""
    from cache import get_photo_id, remember_photo
    from pathlib import Path

    if not image:
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=ack_message_id,
                text=text[:4096],
                reply_markup=keyboard,
                parse_mode=ParseMode.MARKDOWN,
                disable_web_page_preview=True,
            )
            return
        except TelegramError:
            pass

    # Delete the ACK message, then send the rich screen fresh.
    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=ack_message_id)
    except TelegramError:
        pass

    cached_id = get_photo_id(image) if image else None
    try:
        if image:
            if cached_id:
                # ── Use cached file_id — no upload, no timeout risk ────────
                await context.bot.send_photo(
                    chat_id=chat_id, photo=cached_id,
                    caption=text[:1024], reply_markup=keyboard,
                    parse_mode=ParseMode.MARKDOWN,
                )
            else:
                # ── First time: upload the file and cache the returned id ──
                p = Path("assets") / image
                if p.exists():
                    with p.open("rb") as fh:
                        msg = await context.bot.send_photo(
                            chat_id=chat_id, photo=fh,
                            caption=text[:1024], reply_markup=keyboard,
                            parse_mode=ParseMode.MARKDOWN,
                        )
                    if msg and msg.photo:
                        # Save the file_id — next send will be instant.
                        remember_photo(image, msg.photo[-1].file_id)
                        logger.info("📸 Cached file_id for %s", image)
                else:
                    # Asset missing — fall back to plain text.
                    await context.bot.send_message(
                        chat_id=chat_id, text=text[:4096],
                        reply_markup=keyboard, parse_mode=ParseMode.MARKDOWN,
                        disable_web_page_preview=True,
                    )
        else:
            await context.bot.send_message(
                chat_id=chat_id, text=text[:4096],
                reply_markup=keyboard, parse_mode=ParseMode.MARKDOWN,
                disable_web_page_preview=True,
            )
    except TelegramError as exc:
        logger.warning("Final reply send failed: %s", exc)


async def _notify_admins_new_ticket(
    context: ContextTypes.DEFAULT_TYPE,
    ticket_id: int,
    user,
    text: str,
    escalated: bool = False,
) -> None:
    pretty_id = format_ticket_id(ticket_id)
    snippet   = text if len(text) <= 400 else text[:400] + "…"
    header    = "🚨 *Escalated by user*" if escalated else "🚨 *New escalated ticket*"
    msg = (
        f"{header} `{pretty_id}`\n"
        f"From: @{user.username or '—'} (`{user.id}`)\n\n"
        f"{snippet}\n\n"
        f"Reply: `/reply {pretty_id} your message`"
    )
    for admin_id in admin.get_admin_ids():
        try:
            await context.bot.send_message(
                chat_id=admin_id, text=msg, parse_mode="Markdown"
            )
        except Exception as exc:      # noqa: BLE001
            logger.info("Could not notify admin %s: %s", admin_id, exc)


# ===========================================================================
# SECTION 9 — BACKGROUND JOBS + GLOBAL ERROR HANDLER
# ===========================================================================


async def daily_auto_close(context: ContextTypes.DEFAULT_TYPE) -> None:
    closed = await arun(lambda: tickets.auto_close_stale(days=7))
    if closed:
        logger.info("Auto-closed %d stale ticket(s)", closed)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Global error handler.

    TimedOut / NetworkError are normal polling noise — log at INFO level and
    swallow. Any other exception is logged at ERROR but does NOT crash the bot
    because we catch it here before it can propagate to run_polling().
    """
    err = context.error

    if isinstance(err, TimedOut):
        # A single getUpdates poll timed out — completely normal, ignore.
        logger.info("Telegram polling timed out (normal) — continuing.")
        return

    if isinstance(err, NetworkError):
        logger.warning("Network error (will retry): %s", err)
        return

    # Real error — log it but keep going.
    logger.exception("Unhandled error: %s", err, exc_info=err)
    log_event("ERROR", "SYSTEM", f"Unhandled: {err}")


# ===========================================================================
# SECTION 10 — APPLICATION FACTORY
# ===========================================================================


def build_application() -> Application:
    app = (
        Application.builder()
        .token(TOKEN)
        .concurrent_updates(True)       # handle callbacks in parallel
        .connect_timeout(30)            # seconds to establish connection
        .read_timeout(30)               # seconds to wait for response
        .write_timeout(30)              # seconds to wait for send
        .pool_timeout(30)               # seconds to wait for connection from pool
        .build()
    )

    app.add_handler(CommandHandler("start",     start_command))
    app.add_handler(CommandHandler("help",      help_command))
    app.add_handler(CommandHandler("safety",    safety_command))

    app.add_handler(CommandHandler("admin",     admin.admin_command))
    app.add_handler(CommandHandler("reply",     admin.reply_command))
    app.add_handler(CommandHandler("status",    admin.status_command))
    app.add_handler(CommandHandler("priority",  admin.priority_command))
    app.add_handler(CommandHandler("broadcast", admin.broadcast_command))

    app.add_handler(CallbackQueryHandler(admin.admin_callback,     pattern=r"^adm_"))
    app.add_handler(CallbackQueryHandler(admin.broadcast_callback, pattern=r"^bcast_"))
    app.add_handler(CallbackQueryHandler(ticket_callback,          pattern=r"^tkt_"))
    app.add_handler(CallbackQueryHandler(user_callback,            pattern=r"^usr_"))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))
    app.add_error_handler(error_handler)

    if app.job_queue:
        app.job_queue.run_daily(daily_auto_close, time=time(hour=3, minute=0))

    return app


# ===========================================================================
# SECTION 11 — MAIN ENTRY POINT
# Startup order:
#   1. Flask thread → port bound immediately (Render needs this within ~30 s)
#   2. DB init + static screen cache
#   3. _BOT_READY flag set → /ready returns 200
#   4. Polling loop with auto-restart on crash
# ===========================================================================


def main() -> None:
    # ── Step 1: bind the port RIGHT AWAY ─────────────────────────────────
    # Render kills the service if the port isn't open within ~30 seconds.
    # Flask starts before anything else so the port is bound immediately.
    flask_thread = threading.Thread(target=_run_flask, daemon=True, name="flask-health")
    flask_thread.start()
    print("🌐 Flask health thread started.")

    # ── Step 2: DB + static caches ────────────────────────────────────────
    init_db()
    _prebuild_static_screens()
    logger.info("Cricway Support Bot — DB and caches ready.")

    # ── Step 3: mark ready ────────────────────────────────────────────────
    _BOT_READY.set()
    print("✅ Bot initialised — /ready now returns 200.")

    # ── Step 4: polling loop with exponential back-off auto-restart ───────
    # If run_polling() ever exits (network failure, unhandled crash), we wait
    # briefly and restart rather than letting the process die.
    restart_delay = 5    # seconds; doubles on each successive crash, cap 60 s
    max_delay     = 60

    while True:
        try:
            logger.info("▶️  Starting Telegram polling…")
            app = build_application()
            # drop_pending_updates=True discards any messages that arrived
            # while the bot was down, preventing a message flood on restart.
            app.run_polling(
                allowed_updates=Update.ALL_TYPES,
                drop_pending_updates=True,
            )
            # run_polling() returned cleanly (e.g. KeyboardInterrupt signal)
            logger.info("Polling stopped cleanly — exiting.")
            break

        except KeyboardInterrupt:
            logger.info("KeyboardInterrupt — shutting down.")
            break

        except Exception as exc:           # noqa: BLE001
            logger.error(
                "💥 Polling crashed: %s — restarting in %d s…", exc, restart_delay
            )
            _time.sleep(restart_delay)
            restart_delay = min(restart_delay * 2, max_delay)


if __name__ == "__main__":
    try:
        print("Starting Cricway Bot (production-stable)…")
        main()
    except Exception as exc:
        print("❌ FATAL STARTUP ERROR:", exc)
        import traceback
        traceback.print_exc()
