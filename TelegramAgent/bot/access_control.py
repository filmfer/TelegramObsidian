"""Sender allowlist: only the owner(s) may use the bot.

Without this gate anyone who discovers the bot's @username can write notes
into the vault, fill the disk and burn the paid LLM quota. The guard runs as
a group -1 TypeHandler, i.e. BEFORE every other handler, and stops
propagation for unknown senders (silently — no reply that confirms the bot
exists).

Configuration (.env):
  TELEGRAM_ALLOWED_USERS  comma-separated Telegram user/chat ids
  TELEGRAM_CHAT_ID        the owner chat — always included in the allowlist
  ALLOW_ANY_USER=true     explicit opt-out (public bot); otherwise an empty
                          allowlist refuses to start (fail closed).
"""
from __future__ import annotations

import logging
import os
from typing import FrozenSet, Optional, Set

from telegram import Update
from telegram.ext import ApplicationHandlerStop, ContextTypes

logger = logging.getLogger(__name__)

_TRUTHY = ("1", "true", "yes")


def parse_allowed_ids(raw_allowed: Optional[str], raw_chat_id: Optional[str]) -> FrozenSet[int]:
    """Parse ids from TELEGRAM_ALLOWED_USERS + TELEGRAM_CHAT_ID. Invalid tokens are logged and skipped."""
    ids: Set[int] = set()
    tokens = (raw_allowed or "").split(",") + [raw_chat_id or ""]
    for token in tokens:
        token = token.strip()
        if not token:
            continue
        try:
            ids.add(int(token))  # group/channel ids are negative — int() keeps the sign
        except ValueError:
            logger.error("Ignoring invalid Telegram id in allowlist: %r", token)
    return frozenset(ids)


def allow_any_user() -> bool:
    return os.getenv("ALLOW_ANY_USER", "").strip().lower() in _TRUTHY


def load_allowed_ids() -> FrozenSet[int]:
    return parse_allowed_ids(os.getenv("TELEGRAM_ALLOWED_USERS"), os.getenv("TELEGRAM_CHAT_ID"))


def require_allowlist(allowed: FrozenSet[int]) -> None:
    """Fail closed at startup: refuse to run with no allowlist unless explicitly public."""
    if allowed or allow_any_user():
        if not allowed:
            logger.warning("ALLOW_ANY_USER=true — the bot accepts updates from ANY Telegram user.")
        return
    raise RuntimeError(
        "No allowlist configured: set TELEGRAM_CHAT_ID and/or TELEGRAM_ALLOWED_USERS in .env "
        "(or ALLOW_ANY_USER=true to run a public bot)."
    )


def is_authorized(update: object, allowed: FrozenSet[int]) -> bool:
    """True if the update's sender (or chat) is in the allowlist; empty allowlist = open."""
    if not allowed:
        return True
    if not isinstance(update, Update):
        return False
    user = update.effective_user
    chat = update.effective_chat
    return bool(
        (user is not None and user.id in allowed)
        or (chat is not None and chat.id in allowed)
    )


_rejected_logged: Set[int] = set()


def make_guard(allowed: FrozenSet[int]):
    """Build the group -1 handler callback that drops unauthorized updates."""

    async def guard(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        if is_authorized(update, allowed):
            return
        sender = getattr(getattr(update, "effective_user", None), "id", None)
        key = sender if isinstance(sender, int) else 0
        if key not in _rejected_logged:  # log each stranger once, avoid log flooding
            _rejected_logged.add(key)
            logger.warning("Rejected update from unauthorized sender id=%s", sender)
        raise ApplicationHandlerStop

    return guard
