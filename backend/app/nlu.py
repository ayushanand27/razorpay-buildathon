"""
Groq-powered intent classification for the human web chat's free-text
fallback (see main.py's POST /nlu/turn). Deliberately narrow: the LLM
picks ONE of a fixed set of tool names and, for `add`, matches
mentioned items against the REAL catalog -- nothing else.

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

from . import catalog

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

ALLOWED_TOOLS = {"browse", "add", "view_cart", "checkout", "clarify"}
ALLOWED_CATEGORIES = {"electronics", "apparel", "home", "stationery"}

FALLBACK_MESSAGE = "Try 'catalog', 'add sku_001', 'cart', or 'checkout'."

_PRICE_CEILING_RE = re.compile(r"\b(?:under|below|less than)\s+(?:rs\.?\s*)?(\d+)", re.IGNORECASE)
_THINK_BLOCK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_MARKDOWN_FENCE_RE = re.compile(r"^```(?:json)?|```$", re.MULTILINE)
_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)

# Any of these keys, if a model emits them despite instructions, are
# stripped before this module's output is trusted by anything else --
# defense in depth on top of the prompt itself.
_BANNED_KEYS = ("price", "price_inr", "amount", "amount_inr", "total_inr", "allow", "confirm")


def _fallback() -> dict:
    return {"tool": "clarify", "message": FALLBACK_MESSAGE}


def _extract_price_ceiling(text: str) -> float | None:
    m = _PRICE_CEILING_RE.search(text)
    return float(m.group(1)) if m else None


def _catalog_summary() -> str:
    return "\n".join(
        f"{p['id']}: {p['name']} ({p['category']}) -- {p['availability']}"
        for p in catalog.list_products()
    )


def _build_prompt(text: str) -> str:
    return (
        "You are a strict intent classifier for a shopping chat -- not a shopping "
        "assistant, not a pricing engine. Pick EXACTLY ONE tool from this fixed list: "
        "browse, add, view_cart, checkout, clarify. Never invent a tool outside this "
        "list. Never include a price or money amount in your response. Never decide "
        "whether a purchase is allowed -- that is not your job and is checked "
        "elsewhere.\n\n"
        f"Catalog (id: name (category) -- availability):\n{_catalog_summary()}\n\n"
        "Respond with ONLY a compact JSON object, no other text, no markdown fence:\n"
        '- browse: {"tool":"browse","category":"<one of electronics|apparel|home|stationery, or null>"}\n'
        '- add: {"tool":"add","items":[{"product_id":"<a real id from the catalog above>","qty":1}]} '
        "(include every distinct item the user mentions; only use ids that literally "
        "appear in the catalog above; never invent an id)\n"
        '- view_cart: {"tool":"view_cart"}\n'
        '- checkout: {"tool":"checkout"} (use for "pay", "checkout", "buy it now", "confirm", "let\'s do it")\n'
        '- clarify: {"tool":"clarify","message":"<one short helpful sentence, no prices>"} '
        "(use this for anything you can't confidently map to the tools above -- e.g. "
        "removing/changing an item's quantity, which this chat doesn't support yet)\n\n"
        f'User: "{text}"'
    )


def _call_groq(text: str) -> dict | None:
    try:
        resp = requests.post(
            GROQ_API_URL,
            headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
            json={
                "model": GROQ_MODEL,
                "max_tokens": MAX_TOKENS,
                "temperature": 0,
                "messages": [{"role": "user", "content": _build_prompt(text)}],
            },
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
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


def parse_turn(text: str) -> dict:
    """Returns a plan dict with a 'tool' key always in ALLOWED_TOOLS.
    Never includes a price, amount, or allow/confirm field."""
    if not GROQ_API_KEY:
        return _fallback()

    plan = _call_groq(text)
    if not plan or plan.get("tool") not in ALLOWED_TOOLS:
        return _fallback()

    for key in _BANNED_KEYS:
        plan.pop(key, None)

    if plan["tool"] == "add":
        items = plan.get("items")
        valid_items = []
        if isinstance(items, list):
            for item in items:
                if not isinstance(item, dict):
                    continue
                pid = item.get("product_id")
                if pid and catalog.get_product(pid):
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
