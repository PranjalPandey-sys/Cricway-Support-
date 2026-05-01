"""Async AI client + response cache + in-flight dedupe.

Three big wins vs. v1:
  1. Uses openai.AsyncOpenAI — no thread blocking
  2. Identical prompts within 5 minutes return the cached reply (zero API call)
  3. Concurrent identical prompts collapse into ONE upstream call
"""
from __future__ import annotations

import logging
import os
import re
from difflib import SequenceMatcher
from typing import Optional

from cache import AI_INFLIGHT, AI_REPLY_CACHE, hash_text
from database import get_setting, log_event

logger = logging.getLogger(__name__)

OPENAI_API_KEY = (
    os.environ.get("GEMINI_API_KEY")
    or os.environ.get("OPENAI_API_KEY")
    or ""
)
OPENAI_BASE_URL = os.environ.get(
    "OPENAI_BASE_URL", "https://generativelanguage.googleapis.com/v1beta/openai/"
)

AI_FALLBACK_TEXT = (
    "🤖 Our AI is currently unavailable. A support agent will respond soon."
)

SYSTEM_PROMPT = (
    "You are the official AI support assistant for Cricway, a modern SaaS platform for cricket fans. "
    "Tone: friendly, professional, with light cricket vibes (the occasional 'good shot!', "
    "'over to you', 'on the front foot' is welcome — but never sacrifice clarity or professionalism). "
    "Keep answers short (2-5 sentences), helpful, and clear. "
    "Never invent account-specific data, balances, or transactions — if asked, say a human agent will follow up. "
    "Never request passwords, OTPs, or full card numbers. "
    "If a request is outside Cricway support scope, politely redirect them. "
    "If you do not have enough information to answer confidently, end your reply with the exact "
    "token [ESCALATE] so the system can route the user to a human."
)

# ---------------------------------------------------------------------------
# FAQ rules (keyword based) — instant in-memory matcher
# ---------------------------------------------------------------------------

FAQ_RULES = [
    {
        "id": "onboarding",
        "keywords": [
            "bonus", "new user", "newcomer", "newbie",
            "signup", "sign up", "register", "registration",
        ],
        "response": (
            "🎁 *Welcome to Cricway, new player!*\n\n"
            "Here's how to get started:\n"
            "1️⃣  Complete your profile and verify your account.\n"
            "2️⃣  Check the *Promotions* tab — new users may be eligible for an onboarding bonus.\n"
            "3️⃣  Make your first deposit to unlock all features.\n\n"
            "Need anything else? Tap 📝 *Ask a Question* and we'll help you out."
        ),
    },
    {
        "id": "support",
        "keywords": ["help", "support", "issue", "problem", "assist", "not working"],
        "response": (
            "🛠 *How support works at Cricway*\n\n"
            "• Send your concern as a message — every message becomes a tracked ticket.\n"
            "• Our system tries an instant FAQ match first.\n"
            "• If that doesn't fit, our AI assistant or a human agent will respond.\n"
            "• Average response time: *5–30 minutes*, 24/7."
        ),
    },
    {
        "id": "contact",
        "keywords": ["contact", "admin", "human", "agent", "staff", "manager"],
        "response": (
            "👨‍💼 *Reach the team*\n\n"
            "Tap 👨‍💼 *Contact Admin* on the main menu, or simply send your question here. "
            "Every message opens a ticket and the admin is notified for issues our automated systems can't resolve."
        ),
    },
    {
        "id": "deposit",
        "keywords": ["deposit", "add money", "top up", "topup", "fund"],
        "response": (
            "💳 *Deposit Help*\n\n"
            "1️⃣  Open Cricway → *Deposit*.\n"
            "2️⃣  Pick your preferred payment method.\n"
            "3️⃣  Enter the amount and confirm.\n"
            "4️⃣  Funds usually reflect within *2–5 minutes*.\n\n"
            "If a deposit hasn't been credited within 30 minutes, send your *transaction ID* here and we'll resolve it."
        ),
    },
    {
        "id": "withdrawal",
        "keywords": ["withdraw", "withdrawal", "cash out", "payout"],
        "response": (
            "🏦 *Withdrawal Help*\n\n"
            "1️⃣  Open Cricway → *Withdraw*.\n"
            "2️⃣  Confirm your verified payment method.\n"
            "3️⃣  Enter the withdrawal amount and confirm.\n"
            "4️⃣  Withdrawals are typically processed within *15–30 minutes*.\n\n"
            "Delayed withdrawal? Share your *withdrawal reference ID* and the finance team will prioritise it."
        ),
    },
]

# Build a flat token → rule index for O(tokens) lookup
_KEYWORD_INDEX: dict[str, dict] = {}
for _rule in FAQ_RULES:
    for _kw in _rule["keywords"]:
        _KEYWORD_INDEX.setdefault(_kw.lower(), _rule)


def faq_match(text: str) -> Optional[dict]:
    if not text:
        return None
    lowered = text.lower()
    # Fast: scan keywords (still small list, but indexed)
    for kw, rule in _KEYWORD_INDEX.items():
        if kw in lowered:
            return rule
    return None


def faq_suggest(text: str, threshold: float = 0.55) -> Optional[dict]:
    """Fuzzy 'did you mean?' suggestion."""
    if not text:
        return None
    lowered = text.lower()
    best, best_score = None, 0.0
    for rule in FAQ_RULES:
        for kw in rule["keywords"]:
            score = SequenceMatcher(None, lowered, kw).ratio()
            if score > best_score:
                best_score = score
                best = rule
    return best if best and best_score >= threshold else None


# ---------------------------------------------------------------------------
# Async OpenAI client
# ---------------------------------------------------------------------------

_async_client = None


def _get_async_client():
    global _async_client
    if _async_client is not None:
        return _async_client
    if not OPENAI_API_KEY:
        return None
    try:
        from openai import AsyncOpenAI
        _async_client = AsyncOpenAI(
            api_key=OPENAI_API_KEY,
            base_url=OPENAI_BASE_URL,
            timeout=20.0,
            max_retries=1,
        )
        return _async_client
    except Exception as exc:  # noqa: BLE001
        logger.error("Failed to init AsyncOpenAI: %s", exc)
        return None


def ai_enabled() -> bool:
    return get_setting("ai_enabled", "1") == "1"


def current_model() -> str:
    return (
        get_setting("ai_model", os.environ.get("AI_MODEL", "gemini-2.5-flash"))
        or "gemini-2.5-flash"
    )


_LOW_CONF_PATTERNS = re.compile(
    r"\b(i (don'?t|do not) know|i'?m not sure|i cannot help|i can'?t help|"
    r"unable to (help|assist)|please contact (support|the (admin|team)))",
    re.IGNORECASE,
)


def _is_low_confidence(text: str) -> bool:
    if not text or len(text.strip()) < 12:
        return True
    if "[ESCALATE]" in text.upper():
        return True
    return bool(_LOW_CONF_PATTERNS.search(text))


# ---------------------------------------------------------------------------
# Public async API
# ---------------------------------------------------------------------------


async def aget_ai_response(user_message: str) -> tuple[str, bool]:
    """Async + cached + dedup'd AI call. Returns (reply, escalate)."""
    if not ai_enabled():
        return AI_FALLBACK_TEXT, True

    key = hash_text(user_message)
    cached = AI_REPLY_CACHE.get(key)
    if cached is not None:
        return cached

    async def _call_upstream() -> tuple[str, bool]:
        client = _get_async_client()
        if client is None:
            return AI_FALLBACK_TEXT, True
        try:
            completion = await client.chat.completions.create(
                model=current_model(),
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_message},
                ],
                temperature=0.4,
                max_tokens=350,
            )
            raw = (completion.choices[0].message.content or "").strip()
            escalate = _is_low_confidence(raw)
            cleaned = raw.replace("[ESCALATE]", "").replace("[escalate]", "").strip()
            if not cleaned:
                return AI_FALLBACK_TEXT, True
            result = (cleaned, escalate)
            # Only cache confident, successful answers
            if not escalate:
                AI_REPLY_CACHE.set(key, result)
            return result
        except Exception as exc:  # noqa: BLE001
            logger.error("AI request failed: %s", exc)
            log_event("ERROR", "AI", f"AI call failed: {exc}")
            return AI_FALLBACK_TEXT, True

    # Collapse concurrent identical prompts → single upstream call
    return await AI_INFLIGHT.run(key, _call_upstream)


# Sync compatibility shim (in case admin.py or tests import the old name)
def get_ai_response(user_message: str) -> tuple[str, bool]:
    import asyncio
    return asyncio.get_event_loop().run_until_complete(aget_ai_response(user_message))
