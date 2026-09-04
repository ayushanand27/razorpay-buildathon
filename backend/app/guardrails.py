"""
Guardrails layer -- the deterministic ALLOW/BLOCK authority for the
HUMAN checkout rail (POST /checkout). No LLM call anywhere in this
module, or anywhere upstream of it in the checkout path.

The AGENT checkout rail (POST /agent/pay) has its own, stricter
deterministic authority -- see policy.py -- which additionally
verifies the caller's signed spending warrant, per-transaction/daily
caps, allowed categories, and a price-tamper check. A human buyer
carries no warrant and isn't capped that way (a human is already the
accountable party), so this module stays intentionally simple: only
the two checks that apply to EVERY buyer, human or agent, live here.
"""

from . import catalog

# Merchant-set ceiling on a single human order -- used only by
# catalog.get_upsell() to avoid suggesting an add-on that would push a
# human buyer's cart past a sane order size. NOT enforced at checkout
# time (out of scope here -- only asked for as an upsell guard).
MAX_ORDER_INR = 10_000


class GuardrailBlocked(Exception):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def check_cart_reviewed(reviewed: bool):
    """Raises GuardrailBlocked if the buyer hasn't reviewed the cart
    (via view_cart / GET /cart/{session_id}) since the last mutation or
    checkout attempt for this session -- the server-enforced half of
    "gated"."""
    if not reviewed:
        raise GuardrailBlocked("cart_not_reviewed")


def check_stock(line_items: list[dict]):
    """Per-line-item stock check -- a cart with ONE item at qty > stock
    must block even when every OTHER item in the same cart has plenty
    of stock. A single min(stock)-across-the-whole-cart check misses
    that case entirely."""
    for li in line_items:
        product = catalog.get_product(li["product_id"])
        if product is None or li["qty"] > product["stock"]:
            raise GuardrailBlocked("out_of_stock")


def check_checkout_allowed(line_items: list[dict]):
    """Raises GuardrailBlocked if this (human-rail) checkout should not
    proceed."""
    check_stock(line_items)
    return True
