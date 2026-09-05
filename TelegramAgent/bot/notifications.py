"""Notification helpers: editable status messages, deadlines, error handling.

Task 8 — the user must NEVER be left without a response:
  - StatusMessage edits one Telegram message through processing stages.
  - run_with_deadline caps long jobs (default 10 min) with an intermediate
    warning after ~100 s, cancelling runaway work cleanly.
  - setup_error_logging + register_error_handler catch anything that slips
    through, log the full traceback to a rotating file, and notify the user
    in friendly language (never a raw stacktrace).
"""
from __future__ import annotations

import asyncio
import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from telegram import Message, Update

logger = logging.getLogger(__name__)

TASK_TIMEOUT_SECONDS = int(os.getenv("TASK_TIMEOUT_SECONDS", "600"))
TASK_WARN_SECONDS = int(os.getenv("TASK_WARN_SECONDS", "100"))

# Sentinel returned by run_with_deadline when the hard timeout was hit.
#
# Educational note — why a private object() instead of None or a string:
# the wrapped coroutine may legitimately return None (or any value), so
# callers MUST be able to distinguish "job returned nothing" from "job was
# cancelled". object() creates a unique identity that no other value can
# ever equal, and callers compare with `result is TIMEOUT` — the `is`
# operator checks identity (same object in memory), which is the correct
# and fast way to test for a sentinel (never use `==` for sentinels).
TIMEOUT = object()


class StatusMessage:
    """One Telegram message that is edited as processing progresses."""

    def __init__(self, message: Message):
        self._message = message
        self._last_text: str = ""

    @property
    def message(self) -> Message:
        return self._message

    async def update(self, text: str) -> None:
        """Edit the status message; identical edits and edit races are swallowed."""
        if text == self._last_text:
            return
        try:
            await self._message.edit_text(text)
            self._last_text = text
        except Exception as e:  # BadRequest: "message is not modified", etc.
            logger.debug("Status edit skipped: %s", e)

    async def fail(self, reason: str) -> None:
        await self.update(f"❌ {reason}")


async def run_with_deadline(status: StatusMessage, coro: Any) -> Any:
    """Await `coro` with a warning checkpoint and a hard timeout.

    Returns TIMEOUT sentinel if cancelled; otherwise re-raises the
    coroutine's exception or returns its result.
    """
    task = asyncio.ensure_future(coro)
    done, _ = await asyncio.wait({task}, timeout=TASK_WARN_SECONDS)
    if not done:
        await status.update(
            "⏳ Still working — this one is taking longer than usual, hang tight…"
        )
        done, _ = await asyncio.wait(
            {task}, timeout=max(TASK_TIMEOUT_SECONDS - TASK_WARN_SECONDS, 1)
        )
    if not done:
        task.cancel()
        try:
            await task
        except BaseException:  # noqa: BLE001 — cancellation cleanup
            pass
        await status.update(
            "⏱️ This task took over 10 minutes and was cancelled.\n"
            "Try again — if it persists, the file may be too large or complex."
        )
        return TIMEOUT
    return task.result()


def setup_error_logging() -> None:
    """Add rotating file + stdout handlers so logs reach both logs/bot.log
    and `docker compose logs` (INFO and up)."""
    log_dir = Path(os.getenv("LOG_DIR", "logs"))
    fmt = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        fh = RotatingFileHandler(
            log_dir / "bot.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8"
        )
        fh.setFormatter(fmt)
        fh.setLevel(logging.INFO)
        root.addHandler(fh)
    except OSError as e:
        logger.warning("Could not open log file: %s — file logging disabled", e)
    # stdout: makes `docker compose logs` show INFO+ (ops visibility)
    if not any(isinstance(h, logging.StreamHandler) and not isinstance(h, RotatingFileHandler)
               for h in root.handlers):
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        sh.setLevel(logging.INFO)
        root.addHandler(sh)


async def on_error(update: object, context) -> None:
    """Global PTB error handler — log everything, answer the user politely."""
    logger.error("Unhandled exception", exc_info=context.error)

    if getattr(context.error, "handled", False):
        return

    message = None
    if isinstance(update, Update):
        message = update.effective_message
    if message is None:
        return

    text = (
        "❌ Something went wrong while processing this. The error was logged.\n"
        "Try sending it again — if it keeps failing, run /models to check AI providers."
    )
    try:
        await message.reply_text(text)
    except Exception as e:  # even the reply may fail (network)
        logger.error("Could not deliver error notice to user: %s", e)
