"""/models — model picker, fallback-chain status and health tools.

Screen:
  🧠 first choice (or 🆓 Auto), last model that answered
  provider status (key present / rejected / missing)
  the fallback order a typical note would use right now
  cooling-down / unavailable / size-limited models
Buttons:
  🆓 Auto · 🧪 Test chain · ♻️ Reset health · one button per model (tap = first choice)

Plain text only (no Markdown parse mode): model ids contain '_' and '*'.
"""
from __future__ import annotations

import logging
from typing import Dict, List, Tuple

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from llm.free_pool import AUTO_MODEL, context_of, is_free, quality
from llm.health import HEALTH
from llm.provider import (
    _provider_from_model,
    _provider_ready,
    candidate_models,
    get_catalog,
    get_current_model,
    get_models_info,
    list_available_models,
    plan_chain,
    probe_models,
    set_current_model,
)

logger = logging.getLogger(__name__)

MAX_MODEL_BUTTONS = 30
PROVIDERS = ("gemini", "groq", "zai", "openrouter", "ollama")
# A "typical note" request used to preview the chain: ~10k chars in, 4k out.
_PREVIEW_INPUT_TOKENS, _PREVIEW_MAX_TOKENS = 3000, 4096
_TEST_COUNT = 6


def _ctx_label(model: str, info: Dict[str, dict]) -> str:
    ctx = context_of(model, info)
    return f" ·{ctx // 1000}k" if ctx else ""


def _fmt_secs(s: float) -> str:
    return f"{s:.0f}s" if s < 120 else f"{s / 60:.0f}min"


def overview_text(catalog: Dict[str, List[str]]) -> str:
    """Status screen for /models (pure formatting over provider + health state)."""
    info = get_models_info()
    current = get_current_model()
    lines = [
        "🧠 First choice: 🆓 Auto — best free model first" if current == AUTO_MODEL
        else f"🧠 First choice: {current}",
    ]
    if HEALTH.last_ok:
        lines.append(f"✅ Last answer from: {HEALTH.last_ok}")

    prov = []
    for p in PROVIDERS:
        if p in HEALTH.auth_failed:
            prov.append(f"🔑❌ {p} (key rejected)")
        elif _provider_ready(p) or (p == "openrouter" and p in catalog):
            prov.append(f"✅ {p} {len(catalog.get(p, [])) or ''}".rstrip())
        else:
            prov.append(f"⚪ {p} (no key)")
    lines.append("Providers: " + " · ".join(prov))

    plan = plan_chain(_PREVIEW_INPUT_TOKENS, _PREVIEW_MAX_TOKENS)
    lines.append("\n🔗 Fallback order now (typical note):")
    if plan["ready"]:
        for i, (m, _fitted) in enumerate(plan["ready"][:8], 1):
            tag = "🆓 " if is_free(m) else "💳 "
            lines.append(f" {i}. {tag}{m}{_ctx_label(m, info)}")
        if len(plan["ready"]) > 8:
            lines.append(f" … +{len(plan['ready']) - 8} more")
    else:
        lines.append(" (none ready — see below)")

    if plan["cooling"]:
        lines.append("\n⏳ Cooling down:")
        for m, left in sorted(plan["cooling"].items(), key=lambda kv: kv[1])[:6]:
            lines.append(f" • {m} — {_fmt_secs(left)} ({HEALTH.cooldown_reason.get(m, '?')})")
    dead = [m for m in candidate_models() if m in HEALTH.dead]
    if dead:
        lines.append("💀 Unavailable this session: " + ", ".join(dead[:6]))
    if plan["size_blocked"]:
        lines.append("📏 Too small for a typical note: " + ", ".join(plan["size_blocked"][:6]))
    return "\n".join(lines)


def build_keyboard(context: ContextTypes.DEFAULT_TYPE,
                   catalog: Dict[str, List[str]]) -> Tuple[InlineKeyboardMarkup, int]:
    """Tools row + one button per model (current first, then free, then capability)."""
    current = get_current_model()
    info = get_models_info()
    models = {m for lst in catalog.values() for m in lst}
    models.update(m for m in candidate_models() if _provider_ready(_provider_from_model(m)))
    flat = sorted(
        models,
        key=lambda m: (m != current, m in HEALTH.dead, not is_free(m), -quality(m), m),
    )[:MAX_MODEL_BUTTONS]
    context.bot_data["model_choices"] = flat

    auto_label = ("✅ " if current == AUTO_MODEL else "") + "🆓 Auto (free models first)"
    rows = [
        [InlineKeyboardButton(auto_label, callback_data="swm:auto")],
        [InlineKeyboardButton("🧪 Test chain", callback_data="mdl:test"),
         InlineKeyboardButton("♻️ Reset health", callback_data="mdl:reset")],
    ]
    for i, m in enumerate(flat):
        mark = "✅ " if m == current else ("💀 " if m in HEALTH.dead else ("🆓 " if is_free(m) else ""))
        rows.append([InlineKeyboardButton(f"{mark}{m}{_ctx_label(m, info)}"[:60],
                                          callback_data=f"swm:{i}")])
    return InlineKeyboardMarkup(rows), len(flat)


async def models_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/models — refresh the catalog, show chain status and the picker."""
    status = await update.message.reply_text("🔍 Checking providers and models…")
    try:
        catalog = await list_available_models()
    except Exception as e:  # discovery is best-effort; fall back to the cached catalog
        logger.warning("Model discovery failed: %s", e)
        catalog = get_catalog()
    markup, count = build_keyboard(context, catalog)
    await status.edit_text(
        f"{overview_text(catalog)}\n\n{count} models — tap one to make it your first choice:",
        reply_markup=markup,
    )


async def offer_model_switch(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """All providers failed — show the status screen so the user can act."""
    try:
        catalog = await list_available_models()
    except Exception as e:
        logger.error("Could not fetch models for fallback UI: %s", e)
        catalog = get_catalog()
    markup, _count = build_keyboard(context, catalog)
    await update.message.reply_text(
        "🚨 AI analysis failed on every model in the chain.\n\n"
        f"{overview_text(catalog)}\n\nTap 🧪 Test chain, or pick a model and resend:",
        reply_markup=markup,
    )


async def model_choice_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """swm:<index> or swm:auto — set the first-choice model."""
    query = update.callback_query
    await query.answer()
    arg = query.data.split(":", 1)[1]
    if arg == "auto":
        model = AUTO_MODEL
    else:
        try:
            model = context.bot_data.get("model_choices", [])[int(arg)]
        except (IndexError, ValueError):
            await query.answer("⚠️ List expired — run /models again.", show_alert=True)
            return
    set_current_model(model)
    HEALTH.dead.pop(model, None)  # explicit user choice deserves a fresh try
    HEALTH.cooldown_until.pop(model, None)
    label = "🆓 Auto — best free model first" if model == AUTO_MODEL else model
    await query.edit_message_text(f"✅ First choice set to:\n{label}\n\nFallbacks still apply.")


async def model_tools_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """mdl:test — ping the first models of the chain; mdl:reset — clear health state."""
    query = update.callback_query
    await query.answer()
    action = query.data.split(":", 1)[1]
    if action == "reset":
        HEALTH.reset()
        await query.edit_message_text(
            "♻️ Health state cleared — every model gets a fresh chance.\nRun /models to see the chain."
        )
        return

    targets = [m for m in candidate_models()
               if HEALTH.usable(m, _provider_from_model(m))][:_TEST_COUNT]
    await query.edit_message_text(f"🧪 Testing {len(targets)} models…")
    results = await probe_models(targets)
    lines = ["🧪 Chain test (tiny prompt):"]
    for r in results:
        lines.append(f"{'✅' if r['ok'] else '❌'} {r['model']} — "
                     f"{r['secs']:.1f}s{'' if r['ok'] else ' [' + r['kind'] + ']'}")
    ok = sum(r["ok"] for r in results)
    lines.append(f"\n{ok}/{len(results)} answering. Failures are now skipped automatically.")
    await query.edit_message_text("\n".join(lines))
