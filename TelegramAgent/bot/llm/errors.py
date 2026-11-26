"""Classify LLM provider errors into actionable kinds.

Each kind maps to a different reaction in the fallback chain:

  AUTH       bad/missing key            → skip the whole provider this session
  NOT_FOUND  model gone/decommissioned  → skip the model this session
  TOO_LARGE  request > context or TPM   → remember the size ceiling; the caller
                                          may split the input (retrying the same
                                          size never helps)
  RATE_LIMIT short window (RPM/TPM)     → cool down for retry-after, then reuse
  QUOTA      daily quota / no credits   → long cool-down
  TRANSIENT  timeout, 5xx, connection   → one quick retry, then short cool-down
  UNKNOWN    anything else              → short cool-down

String matching is deliberate: litellm normalises most providers to OpenAI
exception types, but Groq reports "request too large" as a 413 *rate_limit*
error and Gemini reports daily vs per-minute quota only in the message text.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Optional


class ErrorKind(str, Enum):
    AUTH = "auth"
    NOT_FOUND = "not_found"
    TOO_LARGE = "too_large"
    RATE_LIMIT = "rate_limit"
    QUOTA = "quota"
    TRANSIENT = "transient"
    EMPTY = "empty"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ClassifiedError:
    kind: ErrorKind
    retry_after: Optional[float] = None  # seconds, when the provider said so
    token_limit: Optional[int] = None    # provider-stated request token cap
    message: str = ""


_TOO_LARGE = (
    "request too large", "reduce your message size", "context_length_exceeded",
    "context length", "context window", "maximum context", "too many tokens",
    "input is too long", "prompt is too long", "exceeds the maximum",
    "string too long", "max_tokens is too large", "requested too many tokens",
)
_AUTH = (
    "invalid api key", "invalid_api_key", "incorrect api key", "api key not valid",
    "authentication", "unauthorized", "permission denied", "no auth credentials",
    "api_key_invalid", "missing api key",
)
_NOT_FOUND = (
    "model_not_found", "does not exist", "is not found", "decommissioned",
    "no endpoints found", "not a valid model", "unknown model", "model not found",
    "has been deprecated", "is not supported for generatecontent",
)
_QUOTA = (
    "per day", "perday", "per_day", "requests per day", "daily", "insufficient_quota",
    "requires more credits", "insufficient credits", "credit balance", "billing",
    "free-models-per-day",
)
_RATE = (
    "rate limit", "rate_limit", "ratelimit", "too many requests", "resource_exhausted",
    "resource has been exhausted", "quota", "tokens per minute", "requests per minute",
)
_TRANSIENT = (
    "timeout", "timed out", "connection", "overloaded", "unavailable",
    "bad gateway", "gateway", "internal server error", "temporarily", "try again later",
)

# "Limit 6000, Requested 9512" (Groq) / "limit: 6000" — the request token cap
_LIMIT_RE = re.compile(r"limit[:\s]+(\d{3,7})\s*,\s*requested[:\s]+(\d{3,7})")
# "try again in 7.66s" / "in 1m30.5s" (Groq), "retry in 12.3s" / "retryDelay": "40s" (Gemini)
_RETRY_MS_RE = re.compile(r"(?:try again|retry)\s+(?:in|after)\s+(?:(\d+)m)?\s*([\d.]+)\s*s")
_RETRY_DELAY_RE = re.compile(r"retry_?delay\W+(\d+(?:\.\d+)?)s")
_RETRY_AFTER_RE = re.compile(r"retry[- ]after\W+(\d+(?:\.\d+)?)")


def _status(exc: BaseException) -> Optional[int]:
    for attr in ("status_code", "code", "http_status"):
        v = getattr(exc, attr, None)
        if isinstance(v, int) and 100 <= v <= 599:
            return v
    resp = getattr(exc, "response", None)
    v = getattr(resp, "status_code", None)
    return v if isinstance(v, int) else None


def _retry_after(text: str, exc: BaseException) -> Optional[float]:
    m = _RETRY_MS_RE.search(text)
    if m:
        return float(m.group(1) or 0) * 60 + float(m.group(2))
    for rx in (_RETRY_DELAY_RE, _RETRY_AFTER_RE):
        m = rx.search(text)
        if m:
            return float(m.group(1))
    headers = getattr(getattr(exc, "response", None), "headers", None)
    try:
        if headers and headers.get("retry-after"):
            return float(headers.get("retry-after"))
    except (TypeError, ValueError):
        pass
    return None


def _has(text: str, needles) -> bool:
    return any(n in text for n in needles)


def classify_error(exc: BaseException) -> ClassifiedError:
    """Map any provider exception to an ErrorKind (+ retry hint / token cap)."""
    text = f"{type(exc).__name__}: {exc}".lower()
    status = _status(exc)
    msg = str(exc)[:300]

    limit_m = _LIMIT_RE.search(text)
    token_limit = int(limit_m.group(1)) if limit_m else None

    if status == 413 or "contextwindowexceeded" in text or _has(text, _TOO_LARGE):
        return ClassifiedError(ErrorKind.TOO_LARGE, token_limit=token_limit, message=msg)
    if status in (401, 403) or "authenticationerror" in text or _has(text, _AUTH):
        return ClassifiedError(ErrorKind.AUTH, message=msg)
    if status == 404 or "notfounderror" in text or _has(text, _NOT_FOUND):
        return ClassifiedError(ErrorKind.NOT_FOUND, message=msg)
    if status == 402 or (_has(text, _QUOTA) and (status == 429 or _has(text, _RATE) or "credit" in text)):
        return ClassifiedError(ErrorKind.QUOTA, retry_after=_retry_after(text, exc), message=msg)
    if status == 429 or "ratelimiterror" in text or _has(text, _RATE):
        return ClassifiedError(ErrorKind.RATE_LIMIT, retry_after=_retry_after(text, exc), message=msg)
    if (status is not None and status >= 500) or _has(text, _TRANSIENT) or \
            type(exc).__name__ in ("Timeout", "APIConnectionError", "TimeoutError"):
        return ClassifiedError(ErrorKind.TRANSIENT, message=msg)
    return ClassifiedError(ErrorKind.UNKNOWN, message=msg)


def estimate_tokens(text: str) -> int:
    """Conservative token estimate (~3.5 chars/token; Portuguese/code run denser than English)."""
    return int(len(text or "") / 3.5) + 1
