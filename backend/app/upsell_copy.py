"""
Optional dynamic upsell copy.

catalog.UPSELL_MAP still deterministically decides WHICH product gets
suggested -- that's a "which SKU" decision an LLM call would be
overkill for. This module only tries to write the one-line *reason*
text tailored to what's actually in the cart, in place of the fixed
string, when an API key is configured.

Uses Groq's free-tier API (OpenAI-compatible chat completions,
Llama-hosted) -- no paid account needed, matching the rest of this
project's "100% free" stack. If a backup key is configured
(GROQ_API_KEY_2, ...), a 429 (rate limit exhausted) on one key is
retried against the next automatically -- see groq_keys.py.

Deliberately isolated from guardrail/payment/audit logic: this is
called from catalog.get_upsell() only, never touches checkout,
guardrails, or the audit trail, and any failure here (missing key,
timeout, network error, bad response) falls back to the static
reason silently -- it can never block or slow down a checkout.
"""

import os
from pathlib import Path

import requests
from dotenv import load_dotenv

from . import groq_keys

# .env lives at the project root, one level above backend/ -- same
# loading pattern as payments.py / webhooks.py. Needed here because
# catalog.py (which imports this module) loads before payments.py
# does in main.py's import order.
load_dotenv(Path(__file__).resolve().parents[2] / ".env")

GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"
# Picked over Groq's openai/gpt-oss-* models, which spend the whole
# token budget on hidden chain-of-thought "reasoning" tokens and can
# return empty content for a task this short. Groq's free-tier model
# lineup changes over time -- check https://console.groq.com/docs/models
# if this ever 404s.
GROQ_MODEL = "qwen/qwen3.8-27b"

# Keep add_to_cart snappy -- the buyer should never notice this call.
REQUEST_TIMEOUT_SECONDS = 3


def generate_reason(cart_items: list[dict], suggested_product_name: str, static_fallback: str) -> str:
    """Returns an LLM-written one-line upsell reason tailored to the
    cart, or static_fallback on ANY failure. Never raises."""
    if not GROQ_API_KEY or not cart_items:
        return static_fallback

    try:
        cart_summary = ", ".join(f"{li['qty']}x {li['name']}" for li in cart_items)
        prompt = (
            f"A shopper's cart currently has: {cart_summary}. Write ONE short, "
            f"natural sentence (under 15 words, no quotes, no emoji) suggesting "
            f'they also add "{suggested_product_name}" -- tailored to what\'s '
            f"actually in their cart."
        )
        def _post(key: str):
            return requests.post(
                GROQ_API_URL,
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json={
                    "model": GROQ_MODEL,
                    "max_tokens": 60,
                    "messages": [{"role": "user", "content": prompt}],
                },
                timeout=REQUEST_TIMEOUT_SECONDS,
            )

        resp = groq_keys.post_with_rotation(_post, GROQ_API_KEY)
        if resp is None:
            return static_fallback
        resp.raise_for_status()
        text = resp.json()["choices"][0]["message"]["content"].strip()
        return text or static_fallback
    except Exception:
        return static_fallback
