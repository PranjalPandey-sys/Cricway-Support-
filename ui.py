"""Screen rendering — photo file_id cache + smart edit-vs-send navigation.

Key changes:
 - First time we send an image, capture its file_id and reuse forever after.
 - When the user taps a button, prefer editing the existing message (caption
   or media) instead of delete+send. That removes ~2 Telegram round-trips per
   navigation.
 - Plain delete+send is the last-resort fallback only.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from telegram import (
    InlineKeyboardMarkup,
    InputMediaPhoto,
    Message,
    Update,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest, TelegramError
from telegram.ext import ContextTypes
from telegram.helpers import escape_markdown

from cache import get_photo_id, remember_photo

logger = logging.getLogger(__name__)

ASSETS_DIR = Path("assets")
SEPARATOR = "━━━━━━━━━━━━━━━━━━━━━━"
CAPTION_LIMIT = 1024
TEXT_LIMIT = 4096


def md(text: str) -> str:
    return escape_markdown(text or "", version=1)


def card(
    title: str,
    subtitle: Optional[str] = None,
    body: Optional[str] = None,
    actions: Optional[str] = None,
    footer: Optional[str] = None,
) -> str:
    lines = [SEPARATOR, f"🏏 *{title}*"]
    if subtitle:
        lines.append(f"_{subtitle}_")
    lines.append(SEPARATOR)
    if body:
        lines.append(body)
    if actions:
        lines.append(SEPARATOR)
        lines.append(f"⚡ {actions}")
    if footer:
        lines.append(SEPARATOR)
        lines.append(footer)
    lines.append(SEPARATOR)
    return "\n".join(lines)


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


# ---------------------------------------------------------------------------
# Photo helpers
# ---------------------------------------------------------------------------


async def _send_photo_smart(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    image_name: str,
    caption: str,
    keyboard: Optional[InlineKeyboardMarkup],
) -> Optional[Message]:
    """Send a photo using cached file_id if available, otherwise upload + cache."""
    cached_id = get_photo_id(image_name)
    if cached_id:
        try:
            return await context.bot.send_photo(
                chat_id=chat_id,
                photo=cached_id,
                caption=caption,
                reply_markup=keyboard,
                parse_mode=ParseMode.MARKDOWN,
            )
        except BadRequest:
            pass  # file_id invalidated → re-upload below

    candidate = ASSETS_DIR / image_name
    if not candidate.exists():
        return None

    with candidate.open("rb") as fh:
        msg = await context.bot.send_photo(
            chat_id=chat_id,
            photo=fh,
            caption=caption,
            reply_markup=keyboard,
            parse_mode=ParseMode.MARKDOWN,
        )
    if msg and msg.photo:
        # Largest size = highest-quality file_id
        remember_photo(image_name, msg.photo[-1].file_id)
    return msg


# ---------------------------------------------------------------------------
# Public: show_screen
# ---------------------------------------------------------------------------


async def show_screen(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    image: Optional[str],
    text: str,
    keyboard: Optional[InlineKeyboardMarkup] = None,
) -> Optional[Message]:
    """Render a screen, preferring in-place edits over delete+resend."""
    chat_id = update.effective_chat.id
    cb = update.callback_query
    prev = cb.message if cb else None

    new_caption = _truncate(text, CAPTION_LIMIT) if image else None
    new_text = _truncate(text, TEXT_LIMIT)

    # ----- Path 1: edit-in-place when navigating between screens -----
    if prev is not None:
        prev_has_photo = bool(prev.photo)
        new_has_photo = bool(image)

        try:
            if new_has_photo and prev_has_photo:
                # photo → photo : edit media
                cached_id = get_photo_id(image)
                if cached_id:
                    await prev.edit_media(
                        media=InputMediaPhoto(
                            media=cached_id,
                            caption=new_caption,
                            parse_mode=ParseMode.MARKDOWN,
                        ),
                        reply_markup=keyboard,
                    )
                    return prev
                # else: file not cached yet → fall through to send + cache
            elif not new_has_photo and not prev_has_photo:
                # text → text : edit text
                await prev.edit_text(
                    text=new_text,
                    reply_markup=keyboard,
                    parse_mode=ParseMode.MARKDOWN,
                    disable_web_page_preview=True,
                )
                return prev
        except BadRequest as exc:
            if "not modified" in str(exc).lower():
                return prev  # nothing changed — done
            # else fall through to delete+send
        except TelegramError:
            pass

        # ----- Path 2: type changed (text↔photo) → delete + send fresh -----
        try:
            await prev.delete()
        except TelegramError:
            pass

    # ----- Path 3: brand-new message (or fallback after failed edit) -----
    try:
        if image:
            msg = await _send_photo_smart(context, chat_id, image, new_caption or "", keyboard)
            if msg is not None:
                return msg
        return await context.bot.send_message(
            chat_id=chat_id,
            text=new_text,
            reply_markup=keyboard,
            parse_mode=ParseMode.MARKDOWN,
            disable_web_page_preview=True,
        )
    except BadRequest as exc:
        logger.warning("Markdown render failed (%s) — sending plain text.", exc)
        return await context.bot.send_message(
            chat_id=chat_id,
            text=new_text,
            reply_markup=keyboard,
            disable_web_page_preview=True,
        )
