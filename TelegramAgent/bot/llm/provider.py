"""Multi-provider LLM layer.

Features:
  - Unified completion through litellm with a fallback chain
  - Dynamic model discovery (Gemini / Groq / OpenRouter / Ollama)
  - Persistent runtime config (chosen model, cached catalog) in config.json
  - Auto-switching health check used by the weekly Telegram job
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)

CONFIG_PATH = Path(os.getenv("AGENT_CONFIG_PATH", "config.json"))

# Static defaults; the weekly/startup health check replaces these
# automatically if the configured model disappears.
DEFAULT_MODEL = os.getenv("LLM_MODEL", "gemini/gemini-flash-latest")
DEFAULT_FALLBACKS = [
    m.strip()
    for m in os.getenv(
        "LLM_FALLBACKS",
        "groq/openai/gpt-oss-120b,groq/llama-3.1-8b-instant,"
                "openrouter/deepseek/deepseek-chat:free,zai/glm-4-flash,"
        "gemini/gemini-pro-latest",
    ).split(",")
    if m.strip()
]

# Name fragments that usually indicate cheaper/free-tier models
_FREE_HINTS = ("flash", "mini", "small", "free", "nitro", "8b")

MODEL_ERROR_PATTERNS = (
    "model_not_found",
    "does not exist",
    "is not found",
    "not supported",
    "decommissioned",
    "404",
)


class AllProvidersFailedError(Exception):
    """Raised when every model in the fallback chain failed."""

    def __init__(self, message: str, attempts: Optional[List[Dict[str, str]]] = None):
        super().__init__(message)
        self.attempts = attempts or []


class ProviderRateLimitError(Exception):
    """Raised when the primary provider and fallbacks are failing due to Rate Limits / Quotas."""
    def __init__(self, message: str, provider: str = "Unknown"):
        super().__init__(message)
        self.provider = provider


# ---------------------------------------------------------------- config ---

def _load_config() -> Dict[str, Any]:
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_config(cfg: Dict[str, Any]) -> None:
    try:
        CONFIG_PATH.write_text(
            json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8"
        )
    except Exception as e:  # pragma: no cover
        logger.error(f"Could not persist config: {e}")


def get_current_model() -> str:
    cfg = _load_config()
    return cfg.get("llm", {}).get("current_model") or DEFAULT_MODEL


def get_fallbacks(current: Optional[str] = None) -> List[str]:
    cur = current or get_current_model()
    return [m for m in DEFAULT_FALLBACKS if m != cur]


def set_current_model(model: str) -> None:
    cfg = _load_config()
    cfg.setdefault("llm", {})["current_model"] = model
    cfg["llm"]["updated_at"] = datetime.now(timezone.utc).isoformat()
    _save_config(cfg)
    logger.info(f"Current LLM model set to: {model}")


def get_catalog() -> Dict[str, List[str]]:
    return _load_config().get("models_catalog", {})


# ------------------------------------------------------------ discovery ----

def _provider_ready(prefix: str) -> bool:
    """Whether we have credentials for a given provider prefix."""
    keys = {
        "gemini": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
        "groq": ("GROQ_API_KEY",),
        "openrouter": ("OPENROUTER_API_KEY",),
                "zai": ("ZHIPU_API_KEY", "ZAI_API_KEY"),
        "ollama": ("OLLAMA_HOST",),
    }
    return any(os.getenv(k) for k in keys.get(prefix, []))


def _provider_from_model(model: str) -> str:
    """Extract the provider prefix from a litellm model id.

    Normalises legacy aliases so litellm 1.83+ recognises them::
        zhipu/...   -> zai/...     (Zhipu / Z.AI rebrand)
    """
    prefix = model.split("/")[0].split(":")[0] if "/" in model else model
    if prefix == "zhipu":
        return "zai"
    return prefix


_RATE_LIMIT_HINTS = (
    "429",
    "quota",
    "rate limit",
    "too many requests",
    "insufficient_quota",
    "queries per day",
)


def _is_rate_limit_error(text: str) -> bool:
    """True when an error string indicates a quota/rate-limit condition."""
    t = text.lower()
    return any(h in t for h in _RATE_LIMIT_HINTS)


async def list_available_models() -> Dict[str, List[str]]:
    """
    Query every configured provider for its live model catalog.
    Returns {provider_prefix: ["provider/model", ...]}.
    Providers without credentials or unreachable are omitted.
    """
    catalog: Dict[str, List[str]] = {}

    # --- Google Gemini ---
    if _provider_ready("gemini"):
        try:
            from google import genai

            client = genai.Client(
                api_key=os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
            )
            names = []
            for m in client.models.list():
                actions = getattr(m, "supported_actions", None) or ["generateContent"]
                if "generateContent" not in actions:
                    continue
                name = m.name.replace("models/", "", 1)
                names.append(f"gemini/{name}")
            catalog["gemini"] = sorted(set(names))
        except Exception as e:
            logger.warning(f"Gemini model listing failed: {e}")

    # --- Groq ---
    if _provider_ready("groq"):
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.get(
                    "https://api.groq.com/openai/v1/models",
                    headers={"Authorization": f"Bearer {os.getenv('GROQ_API_KEY')}"},
                )
                r.raise_for_status()
                catalog["groq"] = sorted(
                    f"groq/{m['id']}"
                    for m in r.json().get("data", [])
                    if any(t in m["id"] for t in ("llama", "gemma", "qwen"))
                )
        except Exception as e:
            logger.warning(f"Groq model listing failed: {e}")

    # --- OpenRouter (free models only) ---
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            headers = {}
            if os.getenv("OPENROUTER_API_KEY"):
                headers["Authorization"] = f"Bearer {os.getenv('OPENROUTER_API_KEY')}"
            r = await client.get("https://openrouter.ai/api/v1/models", headers=headers)
            r.raise_for_status()
            free_ids = [
                m["id"]
                for m in r.json().get("data", [])
                if ":free" in m.get("id", "")
            ]
            if free_ids:
                catalog["openrouter"] = sorted(f"openrouter/{i}" for i in free_ids)
    except Exception as e:
        logger.debug(f"OpenRouter model listing skipped: {e}")

    # --- Local Ollama ---
    ollama_host = os.getenv("OLLAMA_HOST")
    if ollama_host:
        try:
            async with httpx.AsyncClient(timeout=3) as client:
                r = await client.get(f"{ollama_host.rstrip('/')}/api/tags")
                r.raise_for_status()
                catalog["ollama"] = sorted(
                    f"ollama/{m['name']}" for m in r.json().get("models", [])
                )
        except Exception:
            logger.debug("Ollama not reachable — skipped.")

    _set_catalog(catalog)
    total = sum(len(v) for v in catalog.values())
    logger.info(f"Model catalog refreshed: {total} models across {len(catalog)} providers")
    return catalog


# ------------------------------------------------------------- selection ---

def _free_score(name: str) -> int:
    low = name.lower()
    score = sum(2 for h in _FREE_HINTS if h in low)
    if ":free" in low:
        score += 3
    return score


def pick_replacement(catalog: Dict[str, List[str]], dead_model: str) -> Optional[str]:
    """Choose best available replacement, preferring same provider + free tier."""
    provider = dead_model.split("/", 1)[0]
    pools: List[List[str]] = []
    if provider in catalog:
        pools.append(catalog[provider])
    pools.append([m for lst in catalog.values() for m in lst])

    for pool in pools:
        usable = [m for m in pool if m != dead_model]
        if usable:
            return max(usable, key=_free_score)
    return None


async def validate_and_autoswitch() -> Optional[str]:
    """
    Refresh catalog; if the current model disappeared, auto-switch.
    Returns a human-readable report (None = nothing changed).
    """
    catalog = await list_available_models()
    if not catalog:
        return None
    flat = [m for lst in catalog.values() for m in lst]
    current = get_current_model()

    if current in flat:
        return None

    replacement = pick_replacement(catalog, current)
    if not replacement:
        logger.warning("Current model unavailable and no replacement found.")
        return (
            f"🚨 Current model *{current}* is unavailable and no alternative "
            f"was found among your providers."
        )

    set_current_model(replacement)
    free_tag = " (free tier)" if _free_score(replacement) > 0 else ""
    return (
        f"⚠️ Model *{current}* is no longer available.\n"
        f"✅ Auto-switched to *{replacement}*{free_tag}.\n"
        f"Use /models to pick a different one."
    )


# ------------------------------------------------------------ completion ---

async def chat(system_prompt: str, user_content: str, max_tokens: int = 8192) -> tuple:
    """
    Run a completion trying the current model first, then fallbacks.

    - Only providers with credentials are attempted (skips dead/no-key noise).
    - If ALL attempted models fail with quota/rate-limit → raises
      ProviderRateLimitError (so the bot tells the user to wait or add a key).
    - Any other mix of failures → AllProvidersFailedError with a per-model
      summary so the caller can show exactly what happened.
    - On successful fallback the working model becomes the new current model
      (persisted), so the quota-trapped model isn't tried again on the next call.
    Returns (text, meta_info) where meta_info = {"model": ..., "usage": ...}.
    """
    current = get_current_model()
    all_models = [current] + [m for m in get_fallbacks(current) if m != current]
    # Only try providers we actually have credentials for.
    chain = [m for m in all_models if _provider_ready(_provider_from_model(m))]
    if not chain:
        chain = all_models  # provider-agnostic configs (custom base_url) stay usable

    attempts: List[Dict[str, str]] = []

    for model in chain:
        try:
            resp, usage_dict = await litellm_acompletion(
                model=model,
                system=system_prompt,
                user=user_content,
                max_tokens=max_tokens,
            )
            if resp:
                # A fallback worked while the current model didn't → persist the
                # switch so the next request doesn't waste a call on the dead quota.
                if model != current:
                    logger.info(f"Auto-switching current model to '{model}' (fallback succeeded)")
                    set_current_model(model)
                return resp, {"model": model, "usage": usage_dict}
            attempts.append({"model": model, "error": "empty response"})
        except Exception as e:
            err = str(e).lower()
            attempts.append({"model": model, "error": str(e)[:200]})
            if any(p in err for p in MODEL_ERROR_PATTERNS):
                logger.warning(f"Model '{model}' appears unavailable: {e}")
                continue  # dead model — straight to next in chain

            # Identify Quota / Rate limit
            if _is_rate_limit_error(err):
                logger.warning(f"Rate limit or Quota exceeded on '{model}': {e}")
                continue  # keep trying fallbacks, but record this as a rate limit

            logger.error(f"Completion failed on '{model}': {e}")

    # Classify the failure mode.
    rate_limits = [a for a in attempts if _is_rate_limit_error(a.get("error", ""))]
    if len(rate_limits) == len(chain) and len(chain) > 0:
        provider = current.split("/")[0] if "/" in current else current
        raise ProviderRateLimitError(
            f"Quota exceeded or Rate Limit hit on all configured providers "
            f"({provider}). Details: {rate_limits[0].get('error')}",
            provider=provider,
        )

    raise AllProvidersFailedError("All LLM providers failed.", attempts)


async def litellm_acompletion(
    model: str, system: str, user: str, max_tokens: int
):
    """Single litellm async completion. Imported lazily to speed startup."""
    import litellm

    response = await litellm.acompletion(
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        temperature=0.4,
        max_tokens=max_tokens,
        num_retries=0,
        timeout=120,
    )
    content = (response.choices[0].message.content or "").strip()
    usage = getattr(response, "usage", None)
    usage_dict = None
    if usage:
        usage_dict = {
            "prompt_tokens": getattr(usage, "prompt_tokens", 0),
            "completion_tokens": getattr(usage, "completion_tokens", 0),
            "total_tokens": getattr(usage, "total_tokens", 0)
        }
    return content, usage_dict

def _set_catalog(catalog: Dict[str, List[str]]) -> None:
    cfg = _load_config()
    cfg["models_catalog"] = catalog
    cfg["last_model_check"] = datetime.now(timezone.utc).isoformat()
    _save_config(cfg)
