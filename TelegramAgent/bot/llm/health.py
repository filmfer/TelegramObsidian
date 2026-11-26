"""In-memory health registry for LLM models (per bot process).

Remembers what each failure taught us so the fallback chain stops wasting
calls — e.g. a 60-chapter book no longer re-hits an exhausted model 60 times:

  * cool-downs after rate-limit / quota / transient errors
  * models that no longer exist, providers whose key is rejected
  * the largest request (input + max_tokens) a model refused → size ceiling,
    used to skip it or shrink max_tokens for later requests

State is deliberately NOT persisted: a restart (or /models → Reset) gives
every model a fresh chance.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, Optional

from llm.errors import ClassifiedError, ErrorKind


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except ValueError:
        return default


RATE_LIMIT_COOLDOWN = _env_float("LLM_RATE_LIMIT_COOLDOWN", 60)
QUOTA_COOLDOWN = _env_float("LLM_QUOTA_COOLDOWN", 3600)
TRANSIENT_COOLDOWN = _env_float("LLM_TRANSIENT_COOLDOWN", 30)
UNKNOWN_COOLDOWN = _env_float("LLM_UNKNOWN_COOLDOWN", 120)
_MAX_COOLDOWN = 6 * 3600


@dataclass
class ModelHealth:
    clock: Callable[[], float] = time.monotonic
    cooldown_until: Dict[str, float] = field(default_factory=dict)
    cooldown_reason: Dict[str, str] = field(default_factory=dict)
    dead: Dict[str, str] = field(default_factory=dict)          # model -> reason
    auth_failed: Dict[str, str] = field(default_factory=dict)   # provider -> reason
    ceiling: Dict[str, int] = field(default_factory=dict)       # model -> refused request tokens
    ok_count: Dict[str, int] = field(default_factory=dict)
    fail_count: Dict[str, int] = field(default_factory=dict)
    last_ok: Optional[str] = None

    # ------------------------------------------------------------ record ---
    def record_success(self, model: str) -> None:
        self.cooldown_until.pop(model, None)
        self.cooldown_reason.pop(model, None)
        self.ok_count[model] = self.ok_count.get(model, 0) + 1
        self.last_ok = model

    def record_failure(self, model: str, provider: str, err: ClassifiedError,
                       request_tokens: int) -> None:
        self.fail_count[model] = self.fail_count.get(model, 0) + 1
        kind = err.kind
        if kind is ErrorKind.AUTH:
            self.auth_failed[provider] = err.message[:120]
        elif kind is ErrorKind.NOT_FOUND:
            self.dead[model] = err.message[:120]
        elif kind is ErrorKind.TOO_LARGE:
            # Provider-stated cap wins; otherwise "this size failed" is the ceiling.
            cap = err.token_limit or request_tokens
            self.ceiling[model] = min(self.ceiling.get(model, cap), cap)
        else:
            default = {
                ErrorKind.RATE_LIMIT: RATE_LIMIT_COOLDOWN,
                ErrorKind.QUOTA: QUOTA_COOLDOWN,
                ErrorKind.TRANSIENT: TRANSIENT_COOLDOWN,
                ErrorKind.EMPTY: TRANSIENT_COOLDOWN,
            }.get(kind, UNKNOWN_COOLDOWN)
            secs = err.retry_after if err.retry_after else default
            if kind is ErrorKind.QUOTA:
                secs = max(secs, default)  # a daily quota never resets in seconds
            self._cool(model, min(max(secs, 5.0), _MAX_COOLDOWN), kind.value)

    def _cool(self, model: str, secs: float, reason: str) -> None:
        until = self.clock() + secs
        if until > self.cooldown_until.get(model, 0):
            self.cooldown_until[model] = until
            self.cooldown_reason[model] = reason

    # ------------------------------------------------------------- query ---
    def cooldown_left(self, model: str) -> float:
        return max(0.0, self.cooldown_until.get(model, 0) - self.clock())

    def usable(self, model: str, provider: str) -> bool:
        """Not dead and provider key not rejected (ignores cool-down and size)."""
        return model not in self.dead and provider not in self.auth_failed

    def max_request(self, model: str) -> Optional[int]:
        """Largest request (tokens) we believe the model accepts, if known."""
        cap = self.ceiling.get(model)
        return cap - 1 if cap else None

    def reset(self) -> None:
        for d in (self.cooldown_until, self.cooldown_reason, self.dead,
                  self.auth_failed, self.ceiling):
            d.clear()


HEALTH = ModelHealth()
