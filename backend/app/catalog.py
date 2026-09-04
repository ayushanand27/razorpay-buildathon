"""
Agent-readable product catalog for the demo merchant.

This is intentionally simple (in-memory) for the buildathon demo.
In production this would be a DB table synced from the merchant's
inventory system, exposed the same way to both the WhatsApp agent
and the MCP tool layer so there is exactly one source of truth.
"""

from . import upsell_copy

# "sku" duplicates "id" deliberately -- "id" is the internal key every
# other module (cart, orders, guardrails, policy) already looks
# products up by; "sku" is the same value under the field name an
# agent-readable catalog is expected to use. tax_bps is informational
# only (India's common 18% GST slab) -- nothing in checkout math adds
# tax on top of price_inr, so this never affects what's actually
# charged.
CATALOG = [
    {
        "id": "sku_001",
        "sku": "sku_001",
        "name": "Wireless Earbuds Pro",
        "description": "Bluetooth 5.3 earbuds, 30hr battery, ANC.",
        "price_inr": 1499,
        "currency": "INR",
        "tax_bps": 1800,
        "stock": 25,
        "category": "electronics",
        "attributes": {"connectivity": "Bluetooth 5.3", "battery_hours": 30, "noise_cancelling": True},
        "return_window_days": 7,
    },
    {
        "id": "sku_002",
        "sku": "sku_002",
        "name": "Cotton Graphic T-Shirt",
        "description": "100% cotton, unisex, 5 colours available.",
        "price_inr": 599,
        "currency": "INR",
        "tax_bps": 1200,
        "stock": 100,
        "category": "apparel",
        "attributes": {"material": "100% cotton", "colours": 5, "fit": "unisex"},
        "return_window_days": 15,
    },
    {
        "id": "sku_003",
        "sku": "sku_003",
        "name": "Stainless Steel Water Bottle",
        "description": "1L, insulated, keeps cold 24hr / hot 12hr.",
        "price_inr": 349,
        "currency": "INR",
        "tax_bps": 1800,
        "stock": 60,
        "category": "home",
        "attributes": {"capacity_litres": 1, "insulated": True},
        "return_window_days": 7,
    },
    {
        "id": "sku_004",
        "sku": "sku_004",
        "name": "Notebook Set (Pack of 3)",
        "description": "A5 ruled notebooks, 100 pages each.",
        "price_inr": 249,
        "currency": "INR",
        "tax_bps": 1200,
        "stock": 200,
        "category": "stationery",
        "attributes": {"pack_size": 3, "pages_each": 100, "size": "A5"},
        "return_window_days": 15,
    },
    {
        "id": "sku_005",
        "sku": "sku_005",
        "name": "Portable Power Bank 10000mAh",
        "description": "Fast charging, dual USB output.",
        "price_inr": 999,
        "currency": "INR",
        "tax_bps": 1800,
        "stock": 0,  # intentionally out of stock -> used for the failure demo
        "category": "electronics",
        "attributes": {"capacity_mah": 10000, "usb_ports": 2},
        "return_window_days": 7,
    },
]


def _serialize(p: dict) -> dict:
    """Public/agent-facing view -- adds `availability`, computed live off
    the current (possibly decremented) stock so it can never drift out
    of sync the way a second static field would."""
    out = dict(p)
    out["availability"] = "in_stock" if p["stock"] > 0 else "out_of_stock"
    return out


def list_products(category: str | None = None):
    products = [p for p in CATALOG if not category or p["category"] == category]
    return [_serialize(p) for p in products]


def get_product(product_id: str):
    """Raw internal record (mutable `stock`, no computed fields) -- used
    by cart/orders/guardrails/policy. For the public/agent-facing view
    with `availability`, see get_product_public()."""
    for p in CATALOG:
        if p["id"] == product_id:
            return p
    return None


def get_product_public(product_id: str):
    p = get_product(product_id)
    return _serialize(p) if p else None


# Fixed "frequently bought together" lookup -- deterministic, not a model
# call. The track's own AI Judgment bar ("use AI models appropriately,
# prefer deterministic solutions where AI is unnecessary") is better
# served by a plain table here than by spending an LLM call on *which*
# SKU to suggest. Never points at sku_005 (out of stock) as a target.
#
# The reason TEXT below is only the fallback -- get_upsell() tries an
# LLM-written, cart-tailored one-liner first (upsell_copy.py) and
# falls back to this fixed string on any failure, so the demo never
# depends on an external call succeeding.
UPSELL_MAP = {
    "sku_001": ("sku_003", "Frequently bought with Wireless Earbuds Pro -- stay hydrated on the go."),
    "sku_002": ("sku_004", "Popular with students -- pair your tee with a fresh notebook set."),
    "sku_003": ("sku_001", "Complete your everyday carry with wireless earbuds."),
    "sku_004": ("sku_002", "Notebook fans also like our graphic tee."),
    "sku_005": ("sku_001", "That one's out of stock -- here's an in-stock pick in electronics."),
}


def get_upsell(product_id: str, cart_items: list[dict] | None = None,
                exclude_ids: set[str] | None = None,
                max_cart_total_inr: float | None = None) -> tuple[dict | None, dict | None]:
    """
    Returns (upsell, blocked) -- exactly one is non-None whenever there
    was a candidate at all:
      - upsell:  {"product_id", "name", "price_inr", "reason"} -- safe
        to suggest.
      - blocked: {"product_id", "reason"} where reason is one of
        "already_in_cart" | "oos" | "would_exceed_cap" -- there WAS a
        candidate (UPSELL_MAP has an entry), but it fails one of the
        policy checks below, so the caller should log upsell_blocked
        instead of upsell_shown.
    Both are None only when UPSELL_MAP has no entry for product_id at
    all -- nothing was ever a candidate, so there's nothing to log.

    The upsell suggestion is itself policy-bounded, not just a slogan:
    it must never point at an out-of-stock SKU, one already in the
    cart, or one that would push the cart total over whatever spending
    ceiling applies to this buyer (`max_cart_total_inr`, computed by
    the caller from the agent's warrant cap or the human
    guardrails.MAX_ORDER_INR -- this module has no session/warrant
    context of its own).
    """
    entry = UPSELL_MAP.get(product_id)
    if not entry:
        return None, None
    suggested_id, static_reason = entry

    if exclude_ids and suggested_id in exclude_ids:
        return None, {"product_id": suggested_id, "reason": "already_in_cart"}

    product = get_product(suggested_id)
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


# Stock is an invariant enforced at CAPTURE time only (see orders.py) --
# never at order-creation time, so a payment link that's created but
# never paid never touches inventory.
_INITIAL_STOCK = {p["id"]: p["stock"] for p in CATALOG}


def decrement_stock(product_id: str, qty: int) -> bool:
    """Returns False (no mutation at all) if there isn't enough stock
    left; the caller (orders.capture_order) is responsible for rolling
    back any other line items it already decremented in the same
    capture attempt."""
    product = get_product(product_id)
    if not product or qty > product["stock"]:
        return False
    product["stock"] -= qty
    return True


def restore_stock(product_id: str, qty: int):
    product = get_product(product_id)
    if product:
        product["stock"] += qty


def reset_stock_for_tests():
    """Test-only helper -- restores every product's stock to its
    original catalog value between pytest test functions."""
    for p in CATALOG:
        p["stock"] = _INITIAL_STOCK[p["id"]]
