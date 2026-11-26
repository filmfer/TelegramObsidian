"""Multi-provider LLM layer.

Features:
  - Unified completion through litellm with a size- and health-aware
    fallback chain: chosen model → LLM_FALLBACKS → free-model pool
  - Error classification (llm/errors.py) + per-model health (llm/health.py):
    cool-downs, dead models, rejected keys, learned request-size ceilings
  - Waits out short rate limits when the caller allows it (max_wait)
  - Dynamic model discovery (Gemini / Groq / OpenRouter / Ollama) with
    context sizes, refreshed lazily every 24h
  - Persistent runtime config (chosen model, cached catalog) in config.json
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx

from llm.errors import ClassifiedError, ErrorKind, classify_error, estimate_tokens
from llm.free_pool import AUTO_MODEL, context_of, free_pool, is_free
from llm.health import HEALTH

logger = logging.getLogger(__name__)

# Runtime config lives in <bot dir>/data/ — the `agent-data` Docker volume —
# so the chosen model survives `docker compose up --build`. The old default
# (cwd/config.json = /app, the image layer) was wiped on every rebuild.
_BOT_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = Path(os.getenv("AGENT_CONFIG_PATH", str(_BOT_DIR / "data" / "config.json")))
_LEGACY_CONFIG_PATH = _BOT_DIR / "config.json"  # read-only fallback for one-time migration

# LLM_MODEL: a litellm model id, or "auto" (free pool first, see llm/free_pool.py).
DEFAULT_MODEL = os.getenv("LLM_MODEL", "gemini/gemini-flash-latest")
# LLM_FALLBACKS: models you explicitly want tried right after LLM_MODEL (e.g. a
# paid model). The free pool is appended automatically — no need to list it.
DEFAULT_FALLBACKS = [
    m.strip() for m in os.getenv("LLM_FALLBACKS", "").split(",") if m.strip()
]

# Name fragments that usually indicate cheaper/free-tier models
_FREE_HINTS = ("flash", "mini", "small", "free", "nitro", "8b")

CATALOG_TTL_SECONDS = 24 * 3600
_MIN_OUTPUT_TOKENS = 256
_SAFETY_TOKENS = 64  # slack between estimated and real token counts


class AllProvidersFailedError(Exception):
    """Raised when every model in the fallback chain failed.

    ``too_large`` is True when request size was (part of) the reason — the
    caller can split the input and retry; resending the same size can't work.
    """

    def __init__(self, message: str, attempts: Optional[List[Dict[str, str]]] = None,
                 too_large: bool = False):
        super().__init__(message)
        self.attempts = attempts or []
        self.too_large = too_large


class ProviderRateLimitError(Exception):
    """Raised when every candidate is rate-limited / out of quota (or cooling down)."""
    def __init__(self, message: str, provider: str = "Unknown",
                 retry_in: Optional[float] = None):
        super().__init__(message)
        self.provider = provider
        self.retry_in = retry_in


# ---------------------------------------------------------------- config ---

def _load_config() -> Dict[str, Any]:
    # Fall back to the legacy location once; the next _save_config writes the new path.
    path = CONFIG_PATH if CONFIG_PATH.exists() or not _LEGACY_CONFIG_PATH.exists() else _LEGACY_CONFIG_PATH
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        logger.warning("Could not read config %s: %s — using defaults", path, e)
        return {}


def _save_config(cfg: Dict[str, Any]) -> None:
    try:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
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


def _list_gemini_sync() -> Tuple[List[str], Dict[str, dict]]:
    """Blocking google-genai listing — run via asyncio.to_thread."""
    from google import genai

    client = genai.Client(api_key=os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY"))
    names, info = [], {}
    for m in client.models.list():
        actions = getattr(m, "supported_actions", None) or ["generateContent"]
        if "generateContent" not in actions:
            continue
        name = "gemini/" + m.name.replace("models/", "", 1)
        names.append(name)
        ctx = getattr(m, "input_token_limit", None)
        if ctx:
            info[name] = {"ctx": int(ctx)}
    return sorted(set(names)), info


async def list_available_models() -> Dict[str, List[str]]:
    """
    Query every configured provider for its live model catalog.
    Returns {provider_prefix: ["provider/model", ...]} and caches it together
    with per-model context sizes ({model: {"ctx": tokens}}) in config.json.
    Providers without credentials or unreachable are omitted.
    """
    catalog: Dict[str, List[str]] = {}
    info: Dict[str, dict] = {}

    # --- Google Gemini --- (sync SDK: keep it off the event loop)
    if _provider_ready("gemini"):
        try:
            names, ginfo = await asyncio.wait_for(asyncio.to_thread(_list_gemini_sync), 20)
            catalog["gemini"] = names
            info.update(ginfo)
        except Exception as e:
            logger.warning(f"Gemini model listing failed: {e}")

    # --- Groq --- (every chat model is on the free tier)
    if _provider_ready("groq"):
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.get(
                    "https://api.groq.com/openai/v1/models",
                    headers={"Authorization": f"Bearer {os.getenv('GROQ_API_KEY')}"},
                )
                r.raise_for_status()
                models = []
                for m in r.json().get("data", []):
                    name = f"groq/{m['id']}"
                    if not is_free(name) or m.get("active") is False:
                        continue  # whisper/guard/tts are not chat models
                    models.append(name)
                    if m.get("context_window"):
                        info[name] = {"ctx": int(m["context_window"])}
                catalog["groq"] = sorted(models)
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
            free = []
            for m in r.json().get("data", []):
                mid = m.get("id", "")
                if not mid.endswith(":free"):
                    continue
                name = f"openrouter/{mid}"
                free.append(name)
                if m.get("context_length"):
                    info[name] = {"ctx": int(m["context_length"])}
            if free:
                catalog["openrouter"] = sorted(free)
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

    _set_catalog(catalog, info)
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

    # "auto" has no fixed model; providers without a listing API (zai) can't be checked.
    if current == AUTO_MODEL or current in flat or _provider_from_model(current) not in catalog:
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

def candidate_models() -> List[str]:
    """Ordered candidates before health/size filtering.

    Fixed model:  chosen model → LLM_FALLBACKS → free pool.
    Auto mode:    free pool → LLM_FALLBACKS (paid models only as last resort).
    """
    current = get_current_model()
    pool = free_pool(get_catalog(), get_models_info())
    fallbacks = [m for m in DEFAULT_FALLBACKS if m != current]
    ordered = pool + fallbacks if current == AUTO_MODEL else [current] + fallbacks + pool
    seen, out = set(), []
    for m in ordered:
        if m not in seen and m != AUTO_MODEL:
            seen.add(m)
            out.append(m)
    ready = [m for m in out if _provider_ready(_provider_from_model(m))]
    return ready or out  # provider-agnostic configs (custom base_url) stay usable


def _request_limit(model: str) -> Optional[int]:
    """Max request tokens (input + output) for a model: learned ceiling ∧ context."""
    limits = [x for x in (HEALTH.max_request(model), context_of(model, get_models_info())) if x]
    return min(limits) if limits else None


def _fit_max_tokens(model: str, input_tokens: int, max_tokens: int) -> Optional[int]:
    """Output budget that fits the model's limit, or None if the input can't fit."""
    limit = _request_limit(model)
    if limit is None:
        return max_tokens
    room = limit - input_tokens - _SAFETY_TOKENS
    floor = min(max_tokens, max(_MIN_OUTPUT_TOKENS, max_tokens // 3))
    return min(max_tokens, room) if room >= floor else None


def plan_chain(input_tokens: int, max_tokens: int) -> Dict[str, Any]:
    """Split candidates into ready / cooling / blocked-by-size for one request."""
    ready: List[Tuple[str, int]] = []
    cooling: Dict[str, float] = {}
    size_blocked: List[str] = []
    for m in candidate_models():
        if not HEALTH.usable(m, _provider_from_model(m)):
            continue
        fitted = _fit_max_tokens(m, input_tokens, max_tokens)
        if fitted is None:
            size_blocked.append(m)
            continue
        left = HEALTH.cooldown_left(m)
        if left > 0:
            cooling[m] = left
        else:
            ready.append((m, fitted))
    return {"ready": ready, "cooling": cooling, "size_blocked": size_blocked}


_catalog_lock = asyncio.Lock()


async def _maybe_refresh_catalog() -> None:
    """Refresh the model catalog at most every CATALOG_TTL_SECONDS (never raises)."""
    last = _load_config().get("last_model_check")
    try:
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(last)).total_seconds() if last else None
    except ValueError:
        age = None
    if age is not None and age < CATALOG_TTL_SECONDS:
        return
    if _catalog_lock.locked():
        return  # another request is refreshing; use the cached catalog meanwhile
    async with _catalog_lock:
        try:
            await asyncio.wait_for(list_available_models(), 45)
        except Exception as e:  # discovery is best-effort; the chain works without it
            logger.warning("Model catalog refresh failed: %s", e)


async def chat(system_prompt: str, user_content: str, max_tokens: int = 8192,
               *, max_wait: float = 0.0) -> tuple:
    """
    Run a completion over the fallback chain (see candidate_models()).

    - Each failure is classified (llm/errors.py) and remembered (llm/health.py):
      cooling-down, dead and size-blocked models are skipped on later calls.
    - max_tokens is shrunk per model to fit its known request limit, so a
      Groq free-tier model (≈6–12k tokens/min) still gets a turn.
    - TRANSIENT errors get one immediate retry on the same model.
    - max_wait > 0: if every candidate is cooling down, sleep until the first
      one recovers (bounded by max_wait) instead of failing — long jobs like
      /book use this to ride out per-minute rate limits.
    - The user's chosen model is never overwritten by a fallback.

    Raises ProviderRateLimitError when only rate limits/quotas stand in the
    way, otherwise AllProvidersFailedError (``too_large`` set when request
    size was a factor). Returns (text, {"model", "usage", "fallback"}).
    """
    await _maybe_refresh_catalog()
    input_tokens = estimate_tokens(system_prompt) + estimate_tokens(user_content)
    deadline = time.monotonic() + max(0.0, max_wait)
    first_choice = get_current_model()
    attempts: List[Dict[str, str]] = []
    too_large = False

    while True:
        plan = plan_chain(input_tokens, max_tokens)
        too_large = too_large or bool(plan["size_blocked"])
        for model, fitted in plan["ready"]:
            provider = _provider_from_model(model)
            for attempt in (1, 2):
                try:
                    resp, usage_dict = await litellm_acompletion(
                        model=model, system=system_prompt, user=user_content,
                        max_tokens=fitted,
                    )
                    if not resp:
                        raise _EmptyResponse("empty response")
                except Exception as e:
                    err = ClassifiedError(ErrorKind.EMPTY, message="empty response") \
                        if isinstance(e, _EmptyResponse) else classify_error(e)
                    if err.kind is ErrorKind.TRANSIENT and attempt == 1:
                        await asyncio.sleep(2)
                        continue  # one quick retry on the same model
                    HEALTH.record_failure(model, provider, err, input_tokens + fitted)
                    too_large = too_large or err.kind is ErrorKind.TOO_LARGE
                    attempts.append({"model": model, "kind": err.kind.value, "error": err.message[:200]})
                    log = logger.error if err.kind in (ErrorKind.AUTH, ErrorKind.UNKNOWN) else logger.warning
                    log("LLM '%s' failed [%s]: %s", model, err.kind.value, err.message[:200])
                    break
                HEALTH.record_success(model)
                if model != first_choice:
                    logger.info("Fallback model '%s' answered (first choice: %s)", model, first_choice)
                return resp, {"model": model, "usage": usage_dict, "fallback": model != first_choice}

        # Nothing answered. Wait for the earliest cool-down if the caller allows it.
        cooling = plan_chain(input_tokens, max_tokens)["cooling"]
        if cooling:
            wait = min(cooling.values()) + 1
            if time.monotonic() + wait <= deadline:
                logger.info("All candidates cooling down — waiting %.0fs", wait)
                await asyncio.sleep(wait)
                continue
        break

    kinds = {a["kind"] for a in attempts}
    # Size problems win: the caller can split the input, waiting won't help.
    if not too_large and (cooling or attempts) \
            and kinds <= {ErrorKind.RATE_LIMIT.value, ErrorKind.QUOTA.value}:
        retry_in = min(cooling.values()) if cooling else None
        detail = attempts[0]["error"] if attempts else "all models are cooling down"
        raise ProviderRateLimitError(
            f"Rate limit / quota on every available model. Details: {detail}",
            provider=_provider_from_model(first_choice), retry_in=retry_in,
        )
    if not attempts:
        attempts.append({"model": "-", "kind": "none",
                         "error": "no usable model (too large, dead, or missing keys)"})
    raise AllProvidersFailedError("All LLM providers failed.", attempts, too_large=too_large)


class _EmptyResponse(Exception):
    pass


async def probe_models(models: List[str], timeout: float = 25) -> List[Dict[str, Any]]:
    """Ping models concurrently with a tiny prompt; results feed HEALTH too."""
    async def one(model: str) -> Dict[str, Any]:
        t0 = time.monotonic()
        try:
            resp, _ = await asyncio.wait_for(
                litellm_acompletion(model=model, system="Reply with OK.", user="ping", max_tokens=5),
                timeout,
            )
            HEALTH.record_success(model)
            return {"model": model, "ok": True, "secs": time.monotonic() - t0, "kind": ""}
        except Exception as e:
            err = classify_error(e)
            HEALTH.record_failure(model, _provider_from_model(model), err, 50)
            return {"model": model, "ok": False, "secs": time.monotonic() - t0, "kind": err.kind.value}

    return list(await asyncio.gather(*(one(m) for m in models)))


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


# Vision-capable models per provider (free tiers). The first with credentials wins;
# then VISION_MODEL env overrides the pick entirely.
_VISION_BY_PROVIDER = (
    ("zai", "zai/glm-4v-flash"),             # Zhipu Z.AI — free GLM-4V
    ("groq", "groq/llama-3.2-90b-vision-preview"),
    ("openrouter", "openrouter/qwen/qwen-2.5-vl-7b-instruct:free"),
)


def default_vision_model() -> str:
    """Pick a vision model for an available provider (or VISION_MODEL override)."""
    override = os.getenv("VISION_MODEL", "").strip()
    if override:
        return override
    for prefix, model in _VISION_BY_PROVIDER:
        if _provider_ready(prefix):
            return model
    return _VISION_BY_PROVIDER[0][1]  # last resort — will fail if no key


def vision_fallback_chain() -> List[str]:
    """Ordered vision models: VISION_MODEL override first, then FREE tiers,
    then the user's paid Gemini (vision-capable) as the last automatic fallback.
    Only providers with credentials are included.
    """
    chain: List[str] = []
    override = os.getenv("VISION_MODEL", "").strip()
    if override:
        chain.append(override)
    for prefix, model in _VISION_BY_PROVIDER:
        if _provider_ready(prefix) and model not in chain:
            chain.append(model)
    # user's paid Gemini — vision-capable, used only after the free tiers fail
    if _provider_ready("gemini") and "gemini/gemini-2.0-flash" not in chain:
        chain.append("gemini/gemini-2.0-flash")
    return chain or [default_vision_model()]


async def chat_vision(
    system_prompt: str,
    user_content: str,
    image_data_uris: List[str],
    max_tokens: int = 4096,
) -> Optional[tuple]:
    """Vision completion: text + one or more base64 data-URI images via litellm.

    Tries every model in ``vision_fallback_chain()`` (VISION_MODEL override →
    free tiers → user's paid Gemini). The caller's active TEXT model is never
    touched — vision calls are stateless. Returns ``(text, {"model": ...})``
    on success, ``None`` when every candidate failed. Callers fall back to OCR.
    """
    import litellm

    chain = vision_fallback_chain()
    content: List[Dict[str, Any]] = [{"type": "text", "text": user_content}]
    for uri in image_data_uris:
        content.append({"type": "image_url", "image_url": {"url": uri}})

    last_error: Optional[Exception] = None
    for model in chain:
        try:
            response = await litellm.acompletion(
                model=model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": content},
                ],
                temperature=0.2,
                max_tokens=max_tokens,
                num_retries=0,
                timeout=180,
            )
            text = (response.choices[0].message.content or "").strip()
            if not text:
                logger.warning("Vision model '%s' returned empty content", model)
                last_error = ValueError("empty vision response")
                continue
            logger.info("Vision extraction succeeded with '%s'", model)
            return text, {"model": model}
        except Exception as e:
            last_error = e
            logger.error(
                "Vision completion failed on '%s': %s [%s]",
                model, e, type(e).__name__, exc_info=True,
            )
    logger.error("All vision models failed (%s); chain=%s", last_error, chain)
    return None

def _set_catalog(catalog: Dict[str, List[str]], info: Optional[Dict[str, dict]] = None) -> None:
    cfg = _load_config()
    cfg["models_catalog"] = catalog
    if info is not None:
        cfg["models_info"] = info
    cfg["last_model_check"] = datetime.now(timezone.utc).isoformat()
    _save_config(cfg)


def get_models_info() -> Dict[str, dict]:
    return _load_config().get("models_info", {})
