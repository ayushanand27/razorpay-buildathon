"""
Agent-readable product catalog -- multi-tenant.

Every function here takes merchant_id explicitly and scopes ALL
lookups by (merchant_id, product_id) together, so two merchants
reusing the same SKU id (see merchants.py -- deliberately set up that
way) never collide. Static catalog definitions live in merchants.py;
this module owns the MUTABLE runtime state (current stock), seeded
from there at import time, plus all query/upsell logic.

This is intentionally simple (in-memory) for the buildathon demo. In
production this would be a DB table synced from each merchant's own
inventory system, exposed the same way to both the WhatsApp agent and
the MCP tool layer so there is exactly one source of truth per
merchant.
"""

from . import merchants, upsell_copy

# {merchant_id: [product dict, ...]} -- deep-ish copies so mutating one
# merchant's stock can never accidentally touch merchants.MERCHANTS
# itself (which stays the static, original definition).
_CATALOGS: dict[str, list[dict]] = {
    mid: [dict(p, attributes=dict(p.get("attributes", {}))) for p in m["catalog"]]
    for mid, m in merchants.MERCHANTS.items()
}

# Stock is an invariant enforced at CAPTURE time only (see orders.py) --
# never at order-creation time, so a payment link/order that's created
# but never paid never touches inventory.
_INITIAL_STOCK: dict[str, dict[str, int]] = {
    mid: {p["id"]: p["stock"] for p in products} for mid, products in _CATALOGS.items()
}


def _serialize(p: dict) -> dict:
    """Public/agent-facing view -- adds `availability`, computed live off
    the current (possibly decremented) stock so it can never drift out
    of sync the way a second static field would."""
    out = dict(p)
    out["availability"] = "in_stock" if p["stock"] > 0 else "out_of_stock"
    return out


def list_products(merchant_id: str, category: str | None = None):
    products = _CATALOGS.get(merchant_id, [])
    filtered = [p for p in products if not category or p["category"] == category]
    return [_serialize(p) for p in filtered]


def get_product(merchant_id: str, product_id: str):
    """Raw internal record (mutable `stock`, no computed fields) -- used
    by cart/orders/guardrails/policy. For the public/agent-facing view
    with `availability`, see get_product_public()."""
    for p in _CATALOGS.get(merchant_id, []):
        if p["id"] == product_id:
            return p
    return None


def get_product_public(merchant_id: str, product_id: str):
    p = get_product(merchant_id, product_id)
    return _serialize(p) if p else None


def get_upsell(merchant_id: str, product_id: str, cart_items: list[dict] | None = None,
                exclude_ids: set[str] | None = None,
                max_cart_total_inr: float | None = None) -> tuple[dict | None, dict | None]:
    """
    Returns (upsell, blocked) -- exactly one is non-None whenever there
    was a candidate at all:
      - upsell:  {"product_id", "name", "price_inr", "reason"} -- safe
        to suggest.
      - blocked: {"product_id", "reason"} where reason is one of
        "already_in_cart" | "oos" | "would_exceed_cap" -- there WAS a
        candidate (this merchant's upsell_map has an entry), but it
        fails one of the policy checks below, so the caller should log
        upsell_blocked instead of upsell_shown.
    Both are None only when this merchant's upsell_map has no entry for
    product_id at all -- nothing was ever a candidate, so there's
    nothing to log.

    The upsell suggestion is itself policy-bounded, not just a slogan:
    it must never point at an out-of-stock SKU, one already in the
    cart, or one that would push the cart total over whatever spending
    ceiling applies to this buyer (`max_cart_total_inr`, computed by
    the caller from the agent's warrant cap or the merchant's
    max_order_inr -- this module has no session/warrant context of its
    own).
    """
    upsell_map = merchants.MERCHANTS.get(merchant_id, {}).get("upsell_map", {})
    entry = upsell_map.get(product_id)
    if not entry:
        return None, None
    suggested_id, static_reason = entry

    if exclude_ids and suggested_id in exclude_ids:
        return None, {"product_id": suggested_id, "reason": "already_in_cart"}

    product = get_product(merchant_id, suggested_id)
    if not product or product["stock"] == 0:
        return None, {"product_id": suggested_id, "reason": "oos"}

    if max_cart_total_inr is not None:
        current_total = sum(li["qty"] * li.get("price_inr", 0) for li in (cart_items or []))
        if current_total + product["price_inr"] > max_cart_total_inr:
            return None, {"product_id": suggested_id, "reason": "would_exceed_cap"}

    reason = upsell_copy.generate_reason(cart_items or [], product["name"], static_reason)
    return {
        "product_id": product["id"],
        "name": product["name"],
        "price_inr": product["price_inr"],
        "reason": reason,
    }, None


def decrement_stock(merchant_id: str, product_id: str, qty: int) -> bool:
    """Returns False (no mutation at all) if there isn't enough stock
    left; the caller (orders.capture_order) is responsible for rolling
    back any other line items it already decremented in the same
    capture attempt."""
    product = get_product(merchant_id, product_id)
    if not product or qty > product["stock"]:
        return False
    product["stock"] -= qty
    return True


def restore_stock(merchant_id: str, product_id: str, qty: int):
    product = get_product(merchant_id, product_id)
    if product:
        product["stock"] += qty


def reset_stock_for_tests(merchant_id: str | None = None):
    """Test-only helper -- restores stock to its original catalog value.
    Resets every merchant if merchant_id is omitted."""
    targets = [merchant_id] if merchant_id else list(_CATALOGS.keys())
    for mid in targets:
        for p in _CATALOGS.get(mid, []):
            p["stock"] = _INITIAL_STOCK[mid][p["id"]]
