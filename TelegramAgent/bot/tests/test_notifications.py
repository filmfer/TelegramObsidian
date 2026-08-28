#!/usr/bin/env python3
"""Tests for notifications.py (Phase 2 — deadlines, status, catch-all).

Run:
    cd TelegramAgent/bot && .venv/bin/python tests/test_notifications.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import notifications  # noqa: E402
from notifications import StatusMessage, TIMEOUT, run_with_deadline  # noqa: E402

failures = 0


def check(name: str, cond: bool) -> None:
    global failures
    print(("✅" if cond else "❌"), name)
    if not cond:
        failures += 1


class FakeStatus:
    """StatusMessage without Telegram — records edited texts."""

    def __init__(self):
        self.texts = []

    async def update(self, text: str) -> None:
        self.texts.append(text)

    async def fail(self, reason: str) -> None:
        await self.update(f"❌ {reason}")


async def main() -> int:
    # Shrink deadlines so the test runs in ~1 s
    notifications.TASK_WARN_SECONDS = 0.2
    notifications.TASK_TIMEOUT_SECONDS = 0.6

    # 1. Fast coroutine: result passes through, no warning
    status = FakeStatus()
    result = await run_with_deadline(status, asyncio.sleep(0.01, result=42))
    check("Fast job returns its result", result == 42)
    check("Fast job sends no warning", len(status.texts) == 0)

    # 2. Medium coroutine: warning sent, still completes
    status = FakeStatus()
    result = await run_with_deadline(status, asyncio.sleep(0.35, result="ok"))
    check("Medium job completes", result == "ok")
    check("Medium job got the warning checkpoint",
          any("Still working" in t for t in status.texts))

    # 3. Runaway coroutine: cancelled, TIMEOUT sentinel, timeout message
    status = FakeStatus()

    async def runaway():
        await asyncio.sleep(30)

    result = await run_with_deadline(status, runaway())
    check("Runaway job returns TIMEOUT", result is TIMEOUT)
    check("Runaway job gets timeout message",
          any("10 minutes" in t for t in status.texts))

    # 4. Exception propagates to caller
    status = FakeStatus()

    async def boom():
        raise ValueError("kaboom")

    try:
        await run_with_deadline(status, boom())
        check("Exception propagates", False)
    except ValueError:
        check("Exception propagates", True)

    # 5. StatusMessage.update dedupes identical texts (with a real instance)
    class FakeTGMessage:
        def __init__(self):
            self.calls = 0

        async def edit_text(self, text):
            self.calls += 1

    sm = StatusMessage(FakeTGMessage())
    await sm.update("same")
    await sm.update("same")
    await sm.update("other")
    check("StatusMessage skips identical edits", sm.message.calls == 2)

    print("\n🎉 ALL NOTIFICATION TESTS PASSED" if failures == 0 else f"\n💥 {failures} FAILURES")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
