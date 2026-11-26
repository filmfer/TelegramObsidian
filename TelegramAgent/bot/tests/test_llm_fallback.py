#!/usr/bin/env python3
"""LLM fallback chain, free pool, health tracking, /book resilience.

litellm is mocked (provider.litellm_acompletion) with error texts in the
formats Groq / Gemini / OpenRouter really return.

Run:
    cd TelegramAgent/bot && .venv/bin/python tests/test_llm_fallback.py
"""
import asyncio
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_TMP = Path(tempfile.mkdtemp(prefix="llm-fallback-test-"))
os.environ.update({
    "TELEGRAM_BOT_TOKEN": "test:token",
    "AGENT_CONFIG_PATH": str(_TMP / "config.json"),
    "DEDUP_DB_PATH": str(_TMP / "agent.db"),
    "BOOK_PROGRESS_DIR": str(_TMP / "book_progress"),
    "OBSIDIAN_VAULT_PATH": str(_TMP / "vault"),
    "GROQ_API_KEY": "x", "GEMINI_API_KEY": "x", "ZAI_API_KEY": "x",
    "LLM_MODEL": "gemini/gemini-flash-latest",
    "LLM_FALLBACKS": "",
})
for k in ("OPENROUTER_API_KEY", "OLLAMA_HOST", "LLM_FREE_MODELS", "ZHIPU_API_KEY"):
    os.environ.pop(k, None)

import llm.provider as prov  # noqa: E402
from llm.errors import ErrorKind, classify_error  # noqa: E402
from llm.free_pool import free_pool  # noqa: E402
from llm.health import HEALTH, ModelHealth  # noqa: E402

PASS, FAIL = "✅", "❌"
results = []


def check(name, cond):
    results.append((name, bool(cond)))
    print(f"{PASS if cond else FAIL} {name}")


class ApiErr(Exception):
    def __init__(self, msg, status=None):
        super().__init__(msg)
        self.status_code = status


class Timeout(Exception):
    pass


class ContextWindowExceededError(Exception):
    pass


GROQ_413 = ApiErr('litellm.RateLimitError: GroqException - {"error":{"message":"Request too large '
                  'for model `llama-3.1-8b-instant` in organization `org_x` service tier `on_demand` '
                  'on tokens per minute (TPM): Limit 6000, Requested 9512, please reduce your message '
                  'size and try again.","type":"tokens","code":"rate_limit_exceeded"}}', 413)
GROQ_TPM = ApiErr("Rate limit reached for model `llama-3.3-70b-versatile` on tokens per minute "
                  "(TPM): Limit 12000, Used 11000, Requested 3000. Please try again in 10.5s.", 429)
GROQ_TPD = ApiErr("Rate limit reached for model `x` on tokens per day (TPD): Limit 500000, Used "
                  "499000, Requested 3000. Please try again in 7m12.5s.", 429)
GEM_RPM = ApiErr('litellm.RateLimitError: VertexAIException - {"error":{"code":429,"message":"You '
                 'exceeded your current quota","status":"RESOURCE_EXHAUSTED","details":[{"quotaId":'
                 '"GenerateRequestsPerMinutePerProjectPerModel-FreeTier"},{"retryDelay": "41s"}]}}', 429)
GEM_RPD = ApiErr('{"error":{"code":429,"status":"RESOURCE_EXHAUSTED","details":[{"quotaId":'
                 '"GenerateRequestsPerDayPerProjectPerModel-FreeTier"}]}}', 429)


# ------------------------------------------------------------- classify ---
def test_classify():
    cases = [
        ("Groq 413 TPM → too_large", GROQ_413, ErrorKind.TOO_LARGE),
        ("Groq TPM 429 → rate_limit", GROQ_TPM, ErrorKind.RATE_LIMIT),
        ("Groq TPD → quota", GROQ_TPD, ErrorKind.QUOTA),
        ("Gemini per-minute → rate_limit", GEM_RPM, ErrorKind.RATE_LIMIT),
        ("Gemini per-day → quota", GEM_RPD, ErrorKind.QUOTA),
        ("OpenRouter 402 credits → quota", ApiErr("This request requires more credits", 402), ErrorKind.QUOTA),
        ("OpenRouter free-models-per-day → quota",
         ApiErr("Rate limit exceeded: free-models-per-day", 429), ErrorKind.QUOTA),
        ("401 → auth", ApiErr("Incorrect API key provided", 401), ErrorKind.AUTH),
        ("decommissioned → not_found",
         ApiErr("The model `x` has been decommissioned and is no longer supported", 400), ErrorKind.NOT_FOUND),
        ("Gemini 404 → not_found", ApiErr("models/x is not found for API version v1beta", 404), ErrorKind.NOT_FOUND),
        ("Timeout → transient", Timeout("Request timed out"), ErrorKind.TRANSIENT),
        ("503 overloaded → transient", ApiErr("The model is overloaded", 503), ErrorKind.TRANSIENT),
        ("ContextWindowExceeded → too_large", ContextWindowExceededError("too long"), ErrorKind.TOO_LARGE),
        ("other → unknown", ValueError("weird"), ErrorKind.UNKNOWN),
    ]
    for name, exc, want in cases:
        got = classify_error(exc).kind
        check(f"classify: {name}" + ("" if got is want else f" (got {got})"), got is want)
    check("Groq 413 token_limit parsed = 6000", classify_error(GROQ_413).token_limit == 6000)
    check("Groq retry 10.5s parsed", classify_error(GROQ_TPM).retry_after == 10.5)
    check("Groq retry 7m12.5s parsed", classify_error(GROQ_TPD).retry_after == 432.5)
    check("Gemini retryDelay 41s parsed", classify_error(GEM_RPM).retry_after == 41)


# --------------------------------------------------------------- health ---
def test_health():
    now = [1000.0]
    h = ModelHealth(clock=lambda: now[0])
    h.record_failure("g/a", "g", classify_error(GROQ_TPM), 100)
    check("rate limit cools down for retry-after", 10 <= h.cooldown_left("g/a") <= 10.5)
    now[0] += 11
    check("cool-down expires", h.cooldown_left("g/a") == 0)
    h.record_failure("g/b", "g", classify_error(GROQ_TPD), 100)
    check("daily quota cools ≥ 1h", h.cooldown_left("g/b") >= 3600)
    h.record_failure("g/c", "g", classify_error(GROQ_413), 9512)
    check("413 sets ceiling from provider limit", h.max_request("g/c") == 5999)
    h.record_failure("z/m", "zai", classify_error(ApiErr("invalid api key", 401)), 10)
    check("auth failure disables provider", not h.usable("z/other", "zai"))
    h.reset()
    check("reset clears everything", h.usable("z/other", "zai") and h.max_request("g/c") is None)


# ------------------------------------------------------------ free pool ---
def test_free_pool():
    catalog = {
        "groq": ["groq/llama-3.3-70b-versatile", "groq/whisper-large-v3", "groq/llama-3.1-8b-instant"],
        "gemini": ["gemini/gemini-flash-latest", "gemini/gemini-pro-latest",
                   "gemini/gemini-2.5-flash-image"],
        "openrouter": [f"openrouter/m{i}:free" for i in range(6)] + ["openrouter/paid"],
    }
    info = {f"openrouter/m{i}:free": {"ctx": (i + 1) * 1000} for i in range(6)}
    pool = free_pool(catalog, info, limit=50)
    check("whisper excluded", "groq/whisper-large-v3" not in pool)
    check("gemini pro / image excluded", not any(("pro" in m or "image" in m) for m in pool if m.startswith("gemini")))
    check("retired curated groq model dropped (not in live catalog)", "groq/openai/gpt-oss-120b" not in pool)
    check("zai curated kept (no catalog)", "zai/glm-4.5-flash" in pool)
    orr = [m for m in pool if m.startswith("openrouter/")]
    check("openrouter capped to 4 largest-context :free",
          orr == ["openrouter/m5:free", "openrouter/m4:free", "openrouter/m3:free", "openrouter/m2:free"])
    check("groq ranked before gemini before openrouter",
          pool.index("groq/llama-3.3-70b-versatile") < pool.index("gemini/gemini-flash-latest")
          < pool.index("openrouter/m5:free"))
    check("70b ranked before 8b", pool.index("groq/llama-3.3-70b-versatile") < pool.index("groq/llama-3.1-8b-instant"))
    with patch.dict(os.environ, {"LLM_FREE_MODELS": "gemini/gemini-flash-latest"}):
        check("LLM_FREE_MODELS pinned first", free_pool(catalog, info)[0] == "gemini/gemini-flash-latest")


# ----------------------------------------------------------------- chat ---
class FakeLLM:
    """Scripted litellm: model -> list of outcomes (Exception or str), consumed in order."""

    def __init__(self, script):
        self.script = {k: list(v) for k, v in script.items()}
        self.calls = []

    async def __call__(self, model, system, user, max_tokens):
        self.calls.append((model, max_tokens))
        outcomes = self.script.get(model) or ["ok from " + model]
        out = outcomes.pop(0) if len(outcomes) > 1 else outcomes[0]
        if isinstance(out, Exception):
            raise out
        return out, {"total_tokens": 1}


def _setup_catalog():
    HEALTH.reset()
    HEALTH.clock = __import__("time").monotonic
    prov._set_catalog(
        {"groq": ["groq/llama-3.3-70b-versatile", "groq/llama-3.1-8b-instant"],
         "gemini": ["gemini/gemini-flash-latest"]},
        {"groq/llama-3.3-70b-versatile": {"ctx": 131072}},
    )
    prov.set_current_model("gemini/gemini-flash-latest")


async def _noop_refresh():
    return None


def run(coro):
    return asyncio.run(coro)


def test_chat():
    with patch.object(prov, "_maybe_refresh_catalog", _noop_refresh):
        # a) first choice rate-limited → next model answers, choice not overwritten
        _setup_catalog()
        fake = FakeLLM({"gemini/gemini-flash-latest": [GEM_RPM]})
        with patch.object(prov, "litellm_acompletion", fake):
            text, meta = run(prov.chat("sys", "hello", max_tokens=1000))
        check("fallback answers after 429", meta["model"] != "gemini/gemini-flash-latest" and meta["fallback"])
        check("user's first choice NOT overwritten", prov.get_current_model() == "gemini/gemini-flash-latest")
        check("rate-limited model cooling ~41s", 30 < HEALTH.cooldown_left("gemini/gemini-flash-latest") <= 41)

        # b) next call skips the cooling model entirely
        fake = FakeLLM({})
        with patch.object(prov, "litellm_acompletion", fake):
            run(prov.chat("sys", "hello", max_tokens=1000))
        check("cooling model skipped on next call", all(m != "gemini/gemini-flash-latest" for m, _ in fake.calls))

        # c) Groq 413 → ceiling learned → later request gets a shrunken max_tokens
        _setup_catalog()
        prov.set_current_model("groq/llama-3.1-8b-instant")
        fake = FakeLLM({"groq/llama-3.1-8b-instant": [GROQ_413, "ok"]})
        with patch.object(prov, "litellm_acompletion", fake):
            run(prov.chat("sys", "x" * 7000, max_tokens=8192))
            first = [c for c in fake.calls if c[0] == "groq/llama-3.1-8b-instant"]
            fake.calls.clear()
            run(prov.chat("sys", "x" * 7000, max_tokens=8192))
        second = [c for c in fake.calls if c[0] == "groq/llama-3.1-8b-instant"]
        check("413 model retried with max_tokens shrunk to fit 6000",
              first and first[0][1] == 8192 and second and second[0][1] < 6000 - 2000)

        # d) everything too large → AllProvidersFailedError.too_large
        _setup_catalog()
        fake = FakeLLM({m: [GROQ_413] for m in prov.candidate_models()})
        with patch.object(prov, "litellm_acompletion", fake):
            try:
                run(prov.chat("sys", "x" * 50000, max_tokens=2000))
                check("all too large → AllProvidersFailedError(too_large)", False)
            except prov.AllProvidersFailedError as e:
                check("all too large → AllProvidersFailedError(too_large)", e.too_large)

        # e) everything rate-limited, no wait allowed → ProviderRateLimitError
        _setup_catalog()
        fake = FakeLLM({m: [GROQ_TPM] for m in prov.candidate_models()})
        with patch.object(prov, "litellm_acompletion", fake):
            try:
                run(prov.chat("sys", "hi", max_tokens=500))
                check("all rate-limited → ProviderRateLimitError", False)
            except prov.ProviderRateLimitError as e:
                check("all rate-limited → ProviderRateLimitError", e.retry_in is not None)

        # f) max_wait: all cooling → sleeps until the first recovers, then succeeds
        _setup_catalog()
        now = [5000.0]
        HEALTH.clock = lambda: now[0]
        slept = []

        async def fake_sleep(secs):
            slept.append(secs)
            now[0] += secs

        fake = FakeLLM({m: [GROQ_TPM, "recovered"] for m in prov.candidate_models()})
        with patch.object(prov, "litellm_acompletion", fake), patch("asyncio.sleep", fake_sleep):
            text, meta = run(prov.chat("sys", "hi", max_tokens=500, max_wait=60))
        check("max_wait: waited for cool-down then answered", text == "recovered" and slept and slept[0] <= 12)

        # g) transient error → one retry on the same model
        _setup_catalog()
        fake = FakeLLM({"gemini/gemini-flash-latest": [Timeout("timed out"), "second try"]})
        with patch.object(prov, "litellm_acompletion", fake), patch("asyncio.sleep", fake_sleep):
            text, meta = run(prov.chat("sys", "hi", max_tokens=500))
        check("transient error retried on same model", text == "second try"
              and meta["model"] == "gemini/gemini-flash-latest")

        # h) auth failure → whole provider skipped afterwards
        _setup_catalog()
        prov.set_current_model("zai/glm-4.5-flash")
        fake = FakeLLM({"zai/glm-4.5-flash": [ApiErr("invalid api key", 401)]})
        with patch.object(prov, "litellm_acompletion", fake):
            run(prov.chat("sys", "hi", max_tokens=500))
            fake.calls.clear()
            run(prov.chat("sys", "hi", max_tokens=500))
        check("rejected key → provider skipped", all(not m.startswith("zai/") for m, _ in fake.calls))

        # i) auto mode → free pool first
        _setup_catalog()
        prov.set_current_model("auto")
        fake = FakeLLM({})
        with patch.object(prov, "litellm_acompletion", fake):
            _t, meta = run(prov.chat("sys", "hi", max_tokens=500))
        check("auto mode starts with best free model (groq 70b)", meta["model"] == "groq/llama-3.3-70b-versatile")
        prov.set_current_model("gemini/gemini-flash-latest")
        HEALTH.clock = __import__("time").monotonic


# --------------------------------------------------------- book helpers ---
def test_book_summarizer():
    import llm.book_summarizer as bs

    async def fake_chat(prompt, text, max_tokens, max_wait):
        if len(text) > 5000:
            raise prov.AllProvidersFailedError("big", [], too_large=True)
        return f"summary of {len(text)} chars\nKEYWORD: a, b", {}

    with patch.object(bs, "chat", fake_chat):
        out = run(bs.summarize_chunk("p", ("para " * 200 + "\n\n") * 12))
        check("too-large chunk auto-halved and joined", out.count("summary of") >= 2)
        check("KEYWORD footer stripped", "KEYWORD" not in out)
        prompts = []

        def mk(part, parts):
            prompts.append((part, parts))
            return "p"
        out = run(bs.summarize_chapter(mk, ("word " * 400 + "\n\n") * 15, chunk_chars=4000))
        check("long chapter summarized in parts (no truncation)",
              len(prompts) > 1 and all(n == len(prompts) for _, n in prompts) and "#### Part 1/" in out)

    from storage.book_progress import clear_progress, load_progress, progress_key, save_progress
    key = progress_key("", "Book", 123)
    save_progress(key, {1: "a", 3: "c"})
    check("checkpoint round-trip", load_progress(key) == {1: "a", 3: "c"})
    clear_progress(key)
    check("checkpoint cleared", load_progress(key) == {})


# ------------------------------------------------- book end-to-end resume ---
class FakeStatus:
    def __init__(self):
        self.texts = []

    async def edit_text(self, text, **_kw):
        self.texts.append(text)


class FakeMessage:
    async def reply_text(self, text, **_kw):
        return FakeStatus()


def test_book_resume():
    import bot
    from types import SimpleNamespace

    raw = "".join(f"Chapter {n}\n\n" + ("Sentence about topic. " * 1200) + "\n\n" for n in range(1, 4))
    meta = {"title": "Test Book", "authors": ["A"], "text": raw, "path": ""}
    upd = SimpleNamespace(message=FakeMessage())
    calls = []
    fail_after_first = [True]

    async def fake_summarize_chapter(make_prompt, text, chunk_chars=0):
        idx = len(calls) + 1
        calls.append(make_prompt(1, 1)[:40])
        if fail_after_first[0] and idx > 1:
            raise prov.ProviderRateLimitError("busy", retry_in=60)
        return f"summary {idx}"

    async def fake_chat(prompt, text, max_tokens=0, max_wait=0):
        return ('META_JSON: {"title": "Test Book — Study Notes", "category": "books", '
                '"tags": ["book"]}\n\n## 🎯 Synopsis\nGood book.'), {}

    with patch.object(bot, "summarize_chapter", fake_summarize_chapter), \
            patch.object(bot, "chat", fake_chat), \
            patch.object(bot, "BOOK_FULLTEXT", False):
        st = FakeStatus()
        run(bot._process_book_task(upd, meta, "book", "", None, st))
        check("run 1 pauses (not 'failed') when models are busy", any("Paused" in t for t in st.texts))
        from storage.book_progress import load_progress, progress_key
        key = progress_key("", "Test Book", len(bot.clean_book_text(raw)))
        saved = load_progress(key)
        check("run 1 checkpointed finished chapter", list(saved) == [1])

        fail_after_first[0] = False
        calls.clear()
        st = FakeStatus()
        run(bot._process_book_task(upd, meta, "book", "", None, st))
        check("run 2 resumes: chapter 1 not re-summarized", len(calls) >= 1 and all("chapter 1 " not in c for c in calls))
        check("run 2 writes the note", any("Study notes ready" in t for t in st.texts))
        check("checkpoint cleared after success", load_progress(key) == {})


# ------------------------------------------------------------ /models UI ---
def test_model_menu():
    from types import SimpleNamespace

    import model_menu as mm

    _setup_catalog()
    HEALTH.record_failure("gemini/gemini-flash-latest", "gemini", classify_error(GEM_RPM), 100)
    HEALTH.record_failure("groq/llama-3.1-8b-instant", "groq",
                          classify_error(ApiErr("model has been decommissioned", 400)), 100)
    catalog = prov.get_catalog()
    text = mm.overview_text(catalog)
    check("overview shows first choice", "First choice: gemini/gemini-flash-latest" in text)
    check("overview shows cooling model + reason", "Cooling down" in text and "rate_limit" in text)
    check("overview shows dead model", "Unavailable this session: groq/llama-3.1-8b-instant" in text)
    check("overview lists provider status", "✅ groq" in text and "⚪ openrouter (no key)" in text)
    check("message fits Telegram limit", len(text) < 3500)

    ctx = SimpleNamespace(bot_data={})
    markup, count = mm.build_keyboard(ctx, catalog)
    rows = markup.inline_keyboard
    check("keyboard: Auto + Test/Reset rows first",
          rows[0][0].callback_data == "swm:auto"
          and [b.callback_data for b in rows[1]] == ["mdl:test", "mdl:reset"])
    check("keyboard: current model listed first with ✅", rows[2][0].text.startswith("✅ gemini/gemini-flash-latest"))
    check("keyboard: callback data ≤ 64 bytes", all(len(b.callback_data) <= 64 for r in rows for b in r))

    edits = []

    async def answer(*_a, **_k):
        return None

    async def edit(text, **_k):
        edits.append(text)

    query = SimpleNamespace(data="swm:auto", answer=answer, edit_message_text=edit)
    run(mm.model_choice_callback(SimpleNamespace(callback_query=query), ctx))
    check("tap Auto sets 'auto' mode", prov.get_current_model() == "auto" and "Auto" in edits[-1])

    query.data = "mdl:reset"
    run(mm.model_tools_callback(SimpleNamespace(callback_query=query), ctx))
    check("Reset clears health", not HEALTH.dead and not HEALTH.cooldown_until)

    async def fake_probe(models, timeout=25):
        return [{"model": m, "ok": i == 0, "secs": 0.4, "kind": "" if i == 0 else "rate_limit"}
                for i, m in enumerate(models)]

    query.data = "mdl:test"
    with patch.object(mm, "probe_models", fake_probe):
        run(mm.model_tools_callback(SimpleNamespace(callback_query=query), ctx))
    check("Test chain reports per-model result", "Chain test" in edits[-1] and "[rate_limit]" in edits[-1])
    prov.set_current_model("gemini/gemini-flash-latest")

    import bot
    src = Path(bot.__file__).read_text(encoding="utf-8")
    check("handlers registered for swm:auto and mdl:*",
          'pattern=r"^swm:(\\d+|auto)$"' in src and 'pattern=r"^mdl:(test|reset)$"' in src)


def main():
    test_classify()
    test_health()
    test_free_pool()
    test_chat()
    test_book_summarizer()
    test_book_resume()
    test_model_menu()
    print(f"\n{sum(1 for _, ok in results if ok)}/{len(results)} checks passed")
    return 0 if all(ok for _, ok in results) else 1


if __name__ == "__main__":
    sys.exit(main())
