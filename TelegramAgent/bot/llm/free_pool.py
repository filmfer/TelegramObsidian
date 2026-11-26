"""Free-model pool: which zero-cost models to fall back to, and in what order.

Sources, merged and de-duplicated:
  1. LLM_FREE_MODELS (env, comma-separated) — explicit order, always first
  2. curated defaults below — dropped when the live catalog for that provider
     is known and no longer lists them (avoids calls to retired models)
  3. the live catalog (/models discovery): every Groq chat model (free tier),
     OpenRouter ``:free`` models, Gemini *flash* models, Z.AI *flash*, Ollama

Ranking: provider reliability first, then a rough capability hint from the
name, then context size. Pure functions — no I/O, no provider imports.
"""
from __future__ import annotations

import os
from typing import Dict, Iterable, List, Optional

AUTO_MODEL = "auto"  # /models "🆓 Auto": free pool first, no fixed primary

DEFAULT_FREE_MODELS = (
    "groq/llama-3.3-70b-versatile",
    "groq/openai/gpt-oss-120b",
    "zai/glm-4.5-flash",
    "zai/glm-4-flash",
    "gemini/gemini-flash-latest",
    "gemini/gemini-flash-lite-latest",
    "groq/openai/gpt-oss-20b",
    "groq/llama-3.1-8b-instant",
    "openrouter/meta-llama/llama-3.3-70b-instruct:free",
)

# Approximate context windows (tokens) for models the catalog may not describe.
KNOWN_CONTEXT = {
    "zai/glm-4-flash": 128_000,
    "zai/glm-4.5-flash": 128_000,
}

# Higher = tried first. Groq/Z.AI: fast + generous free tiers; OpenRouter
# :free is capped per day without credits; local Ollama is slow on CPU.
PROVIDER_PRIORITY = {"groq": 40, "zai": 35, "gemini": 30, "openrouter": 20, "ollama": 10}

# Openrouter lists dozens of :free models; trying them all would make one
# failed request take minutes. Keep the largest-context few.
OPENROUTER_FREE_MAX = 4

_GROQ_NON_CHAT = ("whisper", "guard", "tts", "playai", "orpheus", "distil", "compound", "allam")
_GEMINI_NON_TEXT = ("image", "tts", "audio", "live", "embedding", "vision", "robotics", "computer-use")

_QUALITY_HINTS = (
    ("235b", 13), ("120b", 12), ("405b", 12), ("70b", 10), ("72b", 10),
    ("glm-4.5", 9), ("flash", 8), ("32b", 7), ("27b", 6), ("20b", 5),
    ("8b", 3), ("mini", 2), ("lite", -2), ("1b", -4), ("3b", -3),
)


def provider_of(model: str) -> str:
    prefix = model.split("/", 1)[0] if "/" in model else model
    return "zai" if prefix == "zhipu" else prefix


def is_free(model: str) -> bool:
    """True for models served on a zero-cost tier."""
    low = model.lower()
    p = provider_of(model)
    if p in ("groq", "ollama"):
        return not any(x in low for x in _GROQ_NON_CHAT)
    if p == "openrouter":
        return low.endswith(":free")
    if p == "zai":
        return "flash" in low
    if p == "gemini":
        return "flash" in low and not any(x in low for x in _GEMINI_NON_TEXT)
    return False


def quality(model: str) -> int:
    low = model.lower()
    return sum(w for hint, w in _QUALITY_HINTS if hint in low)


def _env_list(name: str) -> List[str]:
    return [m.strip() for m in os.getenv(name, "").split(",") if m.strip()]


def free_pool(catalog: Dict[str, List[str]], info: Dict[str, dict],
              limit: Optional[int] = None) -> List[str]:
    """Ordered free models. ``catalog``: {provider: [models]}, ``info``: {model: {"ctx": int}}."""
    if limit is None:
        try:
            limit = int(os.getenv("LLM_FREE_POOL_MAX", "10"))
        except ValueError:
            limit = 10

    explicit = _env_list("LLM_FREE_MODELS")

    curated = [
        m for m in DEFAULT_FREE_MODELS
        if provider_of(m) not in catalog or m in catalog[provider_of(m)]
    ]
    live: List[str] = []
    for provider, models in catalog.items():
        free = [m for m in models if is_free(m)]
        if provider == "openrouter":
            free.sort(key=lambda m: -(info.get(m, {}).get("ctx") or 0))
            free = free[:OPENROUTER_FREE_MAX]
        live.extend(free)

    ranked = sorted(
        _dedup(curated + live),
        key=lambda m: (
            -PROVIDER_PRIORITY.get(provider_of(m), 0),
            -quality(m),
            -(context_of(m, info) or 0),
            m,
        ),
    )
    return _dedup(explicit + ranked)[:limit]


def context_of(model: str, info: Dict[str, dict]) -> Optional[int]:
    ctx = info.get(model, {}).get("ctx")
    return int(ctx) if ctx else KNOWN_CONTEXT.get(model)


def _dedup(items: Iterable[str]) -> List[str]:
    seen, out = set(), []
    for m in items:
        if m not in seen:
            seen.add(m)
            out.append(m)
    return out
