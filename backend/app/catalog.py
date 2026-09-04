"""
Agent-readable product catalog -- multi-tenant, backed by db.py's
shared database (catalog_products, upsell_map tables), not an
in-memory dict. Every function takes merchant_id explicitly and scopes
ALL lookups by (merchant_id, product_id) together, so two merchants
reusing the same SKU id never collide, and stock now genuinely
persists across a backend restart (it used to reset every time, back
when the catalog was in-memory).

The two demo merchants' STARTING catalogs are seed data
(db.seed_default_merchants(), called once at app startup) -- this
module has no hardcoded product list of its own; a merchant created at
runtime (merchant_registry.create_merchant) simply starts with zero
rows here until products are added.
"""

from sqlmodel import select

from . import db, upsell_copy


def _product_to_dict(p: "db.CatalogProduct") -> dict:
    return {
        "id": p.product_id,
        "sku": p.product_id,
        "name": p.name,
        "description": p.description,
        "price_inr": p.price_inr,
        "currency": p.currency,
        "tax_bps": p.tax_bps,
        "stock": p.stock,
        "category": p.category,
        "attributes": db.json_loads(p.attributes_json),
        "return_window_days": p.return_window_days,
    }


def _serialize(product: dict) -> dict:
    """Public/agent-facing view -- adds `availability`, computed live off
    the current (possibly decremented) stock so it can never drift out
    of sync the way a second static field would."""
    out = dict(product)
    out["availability"] = "in_stock" if product["stock"] > 0 else "out_of_stock"
    return out


def list_products(merchant_id: str, category: str | None = None) -> list[dict]:
    with db.get_session() as s:
        query = select(db.CatalogProduct).where(db.CatalogProduct.merchant_id == merchant_id)
        if category:
            query = query.where(db.CatalogProduct.category == category)
        rows = s.exec(query).all()
        return [_serialize(_product_to_dict(p)) for p in rows]


def get_product(merchant_id: str, product_id: str) -> dict | None:
    """Raw internal record -- used by cart/orders/guardrails/policy. For
    the public/agent-facing view with `availability`, see
    get_product_public()."""
    with db.get_session() as s:
        p = s.get(db.CatalogProduct, (merchant_id, product_id))
        return _product_to_dict(p) if p else None


def get_product_public(merchant_id: str, product_id: str) -> dict | None:
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
    product_id at all -- nothing was ever a candidate.

    The upsell suggestion is itself policy-bounded, not just a slogan:
    it must never point at an out-of-stock SKU, one already in the
    cart, or one that would push the cart total over whatever spending
    ceiling applies to this buyer (`max_cart_total_inr`, computed by
    the caller from the agent's warrant cap or the merchant's
    max_order_inr -- this module has no session/warrant context of its
    own).
    """
    with db.get_session() as s:
        entry = s.get(db.UpsellMapEntry, (merchant_id, product_id))
        if not entry:
            return None, None
        suggested_id, static_reason = entry.to_product_id, entry.static_reason

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
    with db.get_session() as s:
        p = s.get(db.CatalogProduct, (merchant_id, product_id))
        if not p or qty > p.stock:
            return False
        p.stock -= qty
        s.add(p)
    return True


def restore_stock(merchant_id: str, product_id: str, qty: int):
    with db.get_session() as s:
        p = s.get(db.CatalogProduct, (merchant_id, product_id))
        if p:
            p.stock += qty
            s.add(p)


def set_stock_for_tests(merchant_id: str, product_id: str, stock: int):
    """Test-only helper -- directly overwrites a product's stock. get_product()
    returns a fresh dict built from a query each call (not a live
    reference into any shared state), so mutating its returned dict --
    the old in-memory-catalog-era pattern -- no longer touches the real
    row; this does the actual write."""
    with db.get_session() as s:
        p = s.get(db.CatalogProduct, (merchant_id, product_id))
        if p:
            p.stock = stock
            s.add(p)


def reset_stock_for_tests(merchant_id: str | None = None):
    """Test-only helper -- restores the two SEED merchants' stock to
    its original catalog value between pytest test functions."""
    db.reset_seed_stock_for_tests(merchant_id)
