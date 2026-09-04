"""
Groq-powered intent classification for the human web chat's free-text
fallback (see main.py's POST /nlu/turn). Deliberately narrow: the LLM
picks ONE of a fixed set of tool names and, for `add`/`remove`,
matches mentioned items against the REAL catalog -- nothing else.

A handful of unambiguous phrasings ("pay", "cart", "catalog") are
matched locally by regex before ever calling Groq at all (see
_try_fast_path) -- Groq's free tier has a shared, easily-exhausted
daily quota (also spent by upsell_copy.py), so every call this
fast-path absorbs is one more call still available for text that
actually needs real language understanding.

It never sees prices in its output contract and its output is never
trusted for money: this module has no access to payments.py, cart
totals are never asked of it, and it makes no ALLOW/BLOCK decision.
Whatever tool it picks is executed by main.py through the exact same
cart/checkout/guardrails/policy functions the hardcoded chat commands
already use -- this module only chooses WHICH of those to call.

A "under N" price ceiling (for browse) is extracted by plain regex
over the RAW user text right here, not asked of or trusted from the
LLM -- it's a display-side filter over the real catalog, never an
amount used in a charge.

On ANY failure (no key, timeout, network error, bad/unparseable
response, an unrecognized tool name) falls back to `clarify` with a
fixed hint -- never guesses at a purchase, never blocks the chat.
"""

import json
import os
import re
from pathlib import Path

import requests
from dotenv import load_dotenv

from . import catalog, groq_keys

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"
# Picked for the same reason as upsell_copy.py: unlike Groq's
# openai/gpt-oss-* or other reasoning-variant models, this one answers
# directly without spending its token budget on a hidden <think>
# block first (verified live, repeatedly, in this project). If a
# future model swap reintroduces that behavior anyway, _call_groq()
# still strips a <think>...</think> block defensively before parsing.
GROQ_MODEL = "qwen/qwen3.8-27b"
REQUEST_TIMEOUT_SECONDS = 3
MAX_TOKENS = 300

ALLOWED_TOOLS = {"browse", "add", "remove", "view_cart", "checkout", "clarify"}
ALLOWED_CATEGORIES = {"electronics", "apparel", "home", "stationery"}

FALLBACK_MESSAGE = "Try 'catalog', 'add sku_001', 'cart', or 'checkout'."

_PRICE_CEILING_RE = re.compile(r"\b(?:under|below|less than)\s+(?:rs\.?\s*)?(\d+)", re.IGNORECASE)
_THINK_BLOCK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_MARKDOWN_FENCE_RE = re.compile(r"^```(?:json)?|```$", re.MULTILINE)
_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)

# Cheap, exact-ish local matches for the handful of intents that don't
# need real language understanding -- tried BEFORE calling Groq at all.
# Groq's free tier has a shared, easily-exhausted daily quota (also
# spent by upsell_copy.py on every add-to-cart); every call this
# fast-path absorbs is one more call still available for genuinely
# ambiguous free text later in the same demo/conversation.
_FAST_PATH_CHECKOUT_RE = re.compile(
    r"^(pay|checkout|check out|buy it now|confirm|let'?s do it|go ahead)[.!]?$", re.IGNORECASE)
_FAST_PATH_VIEW_CART_RE = re.compile(
    r"^(view cart|show cart|my cart|what'?s in my cart|cart)[.!]?$", re.IGNORECASE)
_FAST_PATH_BROWSE_RE = re.compile(
    r"^(catalog|browse|show me (everything|the catalog|products)|what do you have)[.!]?$", re.IGNORECASE)


def _try_fast_path(text: str) -> dict | None:
    stripped = text.strip()
    if _FAST_PATH_CHECKOUT_RE.match(stripped):
        return {"tool": "checkout"}
    if _FAST_PATH_VIEW_CART_RE.match(stripped):
        return {"tool": "view_cart"}
    if _FAST_PATH_BROWSE_RE.match(stripped):
        return {"tool": "browse", "category": None}
    return None

# Any of these keys, if a model emits them despite instructions, are
# stripped before this module's output is trusted by anything else --
# defense in depth on top of the prompt itself.
_BANNED_KEYS = ("price", "price_inr", "amount", "amount_inr", "total_inr", "allow", "confirm")


def _fallback() -> dict:
    return {"tool": "clarify", "message": FALLBACK_MESSAGE}


def _extract_price_ceiling(text: str) -> float | None:
    m = _PRICE_CEILING_RE.search(text)
    return float(m.group(1)) if m else None


def _catalog_summary(merchant_id: str) -> str:
    return "\n".join(
        f"{p['id']}: {p['name']} ({p['category']}) -- {p['availability']}"
        for p in catalog.list_products(merchant_id)
    )


def _build_prompt(merchant_id: str, text: str) -> str:
    return (
        "You are a strict intent classifier for a shopping chat -- not a shopping "
        "assistant, not a pricing engine. Pick EXACTLY ONE tool from this fixed list: "
        "browse, add, remove, view_cart, checkout, clarify. Never invent a tool outside "
        "this list. Never include a price or money amount in your response. Never decide "
        "whether a purchase is allowed -- that is not your job and is checked "
        "elsewhere.\n\n"
        f"Catalog (id: name (category) -- availability):\n{_catalog_summary(merchant_id)}\n\n"
        "Respond with ONLY a compact JSON object, no other text, no markdown fence:\n"
        '- browse: {"tool":"browse","category":"<one of electronics|apparel|home|stationery, or null>"} '
        "(use this whenever the user is asking what's available, browsing, or filtering by "
        "budget/use-case, even if no exact category fits -- category:null is fine, do NOT "
        "fall back to clarify just because you can't pin a category or the user mentioned a "
        "price limit; the price limit is handled separately, outside your response)\n"
        '- add: {"tool":"add","items":[{"product_id":"<a real id from the catalog above>","qty":1}]} '
        "(include every distinct item the user mentions; only use ids that literally "
        "appear in the catalog above; never invent an id)\n"
        '- remove: {"tool":"remove","items":[{"product_id":"<a real id from the catalog above>"}]} '
        "(use for \"remove X\", \"take out X\", \"that's too much, remove X\", \"I don't want X "
        "anymore\" -- only for items that make sense to have been added, quantity is ignored, "
        "the whole line item is removed)\n"
        '- view_cart: {"tool":"view_cart"}\n'
        '- checkout: {"tool":"checkout"} (use for "pay", "checkout", "buy it now", "confirm", "let\'s do it")\n'
        '- clarify: {"tool":"clarify","message":"<one short helpful sentence, no prices>"} '
        "(use this ONLY for something none of the tools above can express, e.g. changing an "
        "item's quantity rather than removing it entirely, or a question unrelated to shopping)\n\n"
        "Examples:\n"
        'User: "got anything for the gym under 400" -> {"tool":"browse","category":null}\n'
        'User: "show me electronics" -> {"tool":"browse","category":"electronics"}\n'
        'User: "that\'s too much, remove the earbuds" -> {"tool":"remove","items":[{"product_id":"sku_001"}]} '
        "(assuming sku_001 is the earbuds in the catalog above)\n\n"
        f'User: "{text}"'
    )


def _post_groq(key: str, merchant_id: str, text: str):
    return requests.post(
        GROQ_API_URL,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json={
            "model": GROQ_MODEL,
            "max_tokens": MAX_TOKENS,
            "temperature": 0,
            "messages": [{"role": "user", "content": _build_prompt(merchant_id, text)}],
        },
        timeout=REQUEST_TIMEOUT_SECONDS,
    )


def _call_groq(merchant_id: str, text: str) -> dict | None:
    try:
        resp = groq_keys.post_with_rotation(_post_groq, GROQ_API_KEY, merchant_id, text)
        if resp is None:
            return None
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"].strip()
        # Some models (reasoning variants) prepend a <think>...</think>
        # block before the actual answer despite instructions not to --
        # strip it rather than assume the chosen model never does this.
        content = _THINK_BLOCK_RE.sub("", content).strip()
        content = _MARKDOWN_FENCE_RE.sub("", content).strip()
        try:
            plan = json.loads(content)
        except json.JSONDecodeError:
            # Last resort: pull the first {...} substring out of
            # whatever surrounding text the model added.
            match = _JSON_OBJECT_RE.search(content)
            if not match:
                return None
            plan = json.loads(match.group(0))
        return plan if isinstance(plan, dict) else None
    except Exception:
        return None


def parse_turn(merchant_id: str, text: str) -> dict:
    """Returns a plan dict with a 'tool' key always in ALLOWED_TOOLS.
    Never includes a price, amount, or allow/confirm field. Scoped to
    merchant_id -- the catalog summary in the prompt, and the
    product_id validation below, only ever see THIS merchant's SKUs."""
    fast = _try_fast_path(text)
    if fast:
        return fast

    if not GROQ_API_KEY:
        return _fallback()

    plan = _call_groq(merchant_id, text)
    if not plan or plan.get("tool") not in ALLOWED_TOOLS:
        return _fallback()

    for key in _BANNED_KEYS:
        plan.pop(key, None)

    if plan["tool"] in ("add", "remove"):
        items = plan.get("items")
        valid_items = []
        if isinstance(items, list):
            for item in items:
                if not isinstance(item, dict):
                    continue
                pid = item.get("product_id")
                if pid and catalog.get_product(merchant_id, pid):
                    qty = item.get("qty", 1)
                    valid_items.append({"product_id": pid, "qty": qty if isinstance(qty, int) and qty > 0 else 1})
        if not valid_items:
            return _fallback()
        plan["items"] = valid_items

    elif plan["tool"] == "browse":
        if plan.get("category") not in ALLOWED_CATEGORIES:
            plan["category"] = None
        ceiling = _extract_price_ceiling(text)
        if ceiling is not None:
            plan["price_ceiling_inr"] = ceiling

    elif plan["tool"] == "clarify":
        if not isinstance(plan.get("message"), str) or not plan["message"].strip():
            plan["message"] = FALLBACK_MESSAGE

    return plan
