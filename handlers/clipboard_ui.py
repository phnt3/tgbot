"""Clipboard bridge screens for PC Remote.

Text-based clipboard access between Telegram and the Windows desktop:

- Read: send the current clipboard text to Telegram.
- Write: the next plain-text message becomes the new clipboard contents.
- Clear: empty the clipboard.

Clipboard content is displayed verbatim: messages are sent without parse_mode,
so no escaping is needed and characters such as ``_`` or ``*`` stay intact.

The write prompt is one-shot and expires; any other action cancels it.
"""

import asyncio
import logging
import time
from typing import Any, Dict, List

from aiogram import types

from clipboard_manager import (
    MAX_TEXT_CHARS,
    clear_clipboard,
    clipboard_available,
    read_clipboard_text,
    write_clipboard_text,
)

logger = logging.getLogger(__name__)

# Telegram messages allow 4096 characters; leave room for the header.
DISPLAY_LIMIT = 3500
# How long a "send the clipboard text" prompt stays armed.
WRITE_TTL_SECONDS = 300

_PENDING_WRITE: Dict[int, float] = {}


def _button(text: str, data: str) -> types.InlineKeyboardButton:
    return types.InlineKeyboardButton(text=text, callback_data=data)


def _chat_id(message: Any) -> int:
    chat = getattr(message, "chat", None)
    return int(getattr(chat, "id", 0) or 0)


def _keyboard() -> types.InlineKeyboardMarkup:
    return types.InlineKeyboardMarkup(
        inline_keyboard=[
            [_button("Read clipboard", "clip_read")],
            [_button("Write clipboard", "clip_write")],
            [_button("Clear clipboard", "clip_clear")],
            [_button("<< Back", "menu_main")],
        ]
    )


def _back_keyboard() -> types.InlineKeyboardMarkup:
    return types.InlineKeyboardMarkup(
        inline_keyboard=[
            [_button("Clipboard", "clip_open")],
            [_button("<< Back", "menu_main")],
        ]
    )


def _prune_pending() -> None:
    """Drop expired write prompts."""
    now = time.time()
    for key, expiry in list(_PENDING_WRITE.items()):
        if expiry < now:
            _PENDING_WRITE.pop(key, None)


def write_pending(chat_id: int) -> bool:
    """Return True when this chat has an armed clipboard write prompt."""
    _prune_pending()
    return int(chat_id) in _PENDING_WRITE


def clear_state() -> None:
    """Forget every pending prompt (used by tests and on cancellation)."""
    _PENDING_WRITE.clear()


def cancel_pending(chat_id: int) -> None:
    """Cancel an armed write prompt for this chat."""
    _PENDING_WRITE.pop(int(chat_id), None)


def _arm_write(chat_id: int) -> None:
    _PENDING_WRITE[chat_id] = time.time() + WRITE_TTL_SECONDS


async def show_clipboard(message: Any) -> None:
    """Show the clipboard screen without touching the clipboard itself."""
    _prune_pending()
    available = clipboard_available()

    lines = ["=== CLIPBOARD ===", ""]
    if available:
        lines.append("Read: copy the Windows clipboard text into this chat.")
        lines.append("Write: send text here and it replaces the clipboard.")
        lines.append("Clear: empty the clipboard.")
        lines.append("")
        lines.append(f"Text limit: {MAX_TEXT_CHARS:,} characters.")
        if write_pending(_chat_id(message)):
            lines.append("A write prompt is active: send text or press Cancel.")
    else:
        lines.append("The clipboard bridge is only available on Windows.")

    keyboard = _keyboard()
    if write_pending(_chat_id(message)):
        keyboard = types.InlineKeyboardMarkup(
            inline_keyboard=[
                [_button("Cancel write", "clip_cancel")],
                [_button("<< Back", "menu_main")],
            ]
        )

    await message.answer(
        "\n".join(lines),
        reply_markup=keyboard,
        disable_web_page_preview=True,
    )


async def read_clipboard(message: Any) -> None:
    """Read the clipboard and send its text to the chat."""
    ok, text, truncated = await asyncio.to_thread(read_clipboard_text)
    if not ok:
        await message.answer(
            f"Clipboard read failed.\n{text}",
            reply_markup=_back_keyboard(),
        )
        return

    if not text:
        await message.answer(
            "The clipboard is empty.",
            reply_markup=_back_keyboard(),
        )
        return

    total = len(text)
    shown = text[:DISPLAY_LIMIT]
    lines: List[str] = ["=== CLIPBOARD TEXT ===", ""]
    lines.append(shown)
    lines.append("")
    if truncated:
        lines.append(f"[truncated at {MAX_TEXT_CHARS:,} characters]")
    elif total > DISPLAY_LIMIT:
        lines.append(
            f"[showing {DISPLAY_LIMIT:,} of {total:,} characters]"
        )
    else:
        lines.append(f"[{total:,} characters]")

    await message.answer(
        "\n".join(lines),
        reply_markup=_back_keyboard(),
        disable_web_page_preview=True,
    )


async def ask_write(message: Any) -> None:
    """Arm the one-shot write prompt."""
    chat_id = _chat_id(message)
    _arm_write(chat_id)
    lines = [
        "=== WRITE CLIPBOARD ===",
        "",
        "Send the text in your next message.",
        "It replaces the current clipboard contents on the PC.",
        "",
        f"This prompt expires in {WRITE_TTL_SECONDS // 60} min.",
    ]
    await message.answer(
        "\n".join(lines),
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[[_button("Cancel", "clip_cancel")]]
        ),
    )


async def clear_clipboard_screen(message: Any) -> None:
    """Empty the clipboard after an explicit press."""
    _PENDING_WRITE.pop(_chat_id(message), None)
    ok, text = await asyncio.to_thread(clear_clipboard)
    mark = "OK" if ok else "FAILED"
    await message.answer(
        f"Clear clipboard: {mark}\n{text}",
        reply_markup=_back_keyboard(),
    )


async def handle_callback(callback: Any, data: str) -> None:
    """Handle clip_* callbacks."""
    message = callback.message
    if message is None:
        await callback.answer("Message is no longer available", show_alert=True)
        return

    if data in ("clip_open", "menu_clipboard"):
        await show_clipboard(message)
        return
    if data == "clip_read":
        await read_clipboard(message)
        return
    if data == "clip_write":
        await ask_write(message)
        return
    if data == "clip_clear":
        await clear_clipboard_screen(message)
        return
    if data == "clip_cancel":
        _PENDING_WRITE.pop(_chat_id(message), None)
        await callback.answer("Write cancelled")
        await show_clipboard(message)
        return

    await callback.answer("Unknown clipboard action", show_alert=True)


async def handle_text(message: Any, state: Any = None) -> bool:
    """Turn the next plain-text message into new clipboard contents.

    Returns ``True`` when the message was consumed by a clipboard prompt.
    """
    from security import is_user_authorized

    user = getattr(message, "from_user", None)
    if user is not None and not is_user_authorized(user.id):
        return False

    chat_id = _chat_id(message)
    _prune_pending()
    if chat_id not in _PENDING_WRITE:
        return False

    text = getattr(message, "text", "") or ""
    if not text.strip():
        _arm_write(chat_id)
        await message.answer(
            "Clipboard not changed: the text is empty. "
            "Send text or press Cancel.",
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[[_button("Cancel", "clip_cancel")]]
            ),
        )
        return True

    _PENDING_WRITE.pop(chat_id, None)
    ok, result = await asyncio.to_thread(write_clipboard_text, text)
    mark = "OK" if ok else "FAILED"
    await message.answer(
        f"Write clipboard: {mark}\n{result}",
        reply_markup=_back_keyboard(),
    )
    return True