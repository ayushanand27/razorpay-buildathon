"""
In-memory cart store, keyed by session id.

Both the WhatsApp flow and the MCP flow call into this same module,
so there is one cart implementation, not two -- avoids the classic
demo bug of the "AI buyer" and "human buyer" secretly running on
different logic.
"""

from .catalog import get_product

_CARTS: dict[str, list[dict]] = {}


def get_cart(session_id: str):
    return _CARTS.get(session_id, [])


def add_to_cart(session_id: str, product_id: str, qty: int = 1):
    product = get_product(product_id)
    if not product:
        return None, "product_not_found"

    cart = _CARTS.setdefault(session_id, [])
    for line in cart:
        if line["product_id"] == product_id:
            line["qty"] += qty
            return cart, None

    cart.append({"product_id": product_id, "name": product["name"],
                 "qty": qty, "price_inr": product["price_inr"]})
    return cart, None


def cart_total(session_id: str) -> float:
    cart = get_cart(session_id)
    return sum(line["qty"] * line["price_inr"] for line in cart)


def clear_cart(session_id: str):
    _CARTS[session_id] = []


# Growth-metrics support: tracks which product_ids have been suggested
# as an upsell for a session, so a later add of that same product can
# be counted as an "accepted" upsell. In-memory like _CARTS above --
# no new DB table, resets with the process just like the cart store.
_SUGGESTED_UPSELLS: dict[str, set[str]] = {}
_upsell_accepted_count = 0


def record_upsell_suggested(session_id: str, product_id: str):
    _SUGGESTED_UPSELLS.setdefault(session_id, set()).add(product_id)


def check_and_record_upsell_acceptance(session_id: str, product_id: str) -> bool:
    """If product_id was previously suggested as an upsell for this
    session, counts this add as an accepted upsell and returns True."""
    global _upsell_accepted_count
    if product_id in _SUGGESTED_UPSELLS.get(session_id, ()):
        _upsell_accepted_count += 1
        return True
    return False


def get_upsell_accepted_count() -> int:
    return _upsell_accepted_count


# "Gated" enforcement support: server-side proof the buyer's cart was
# reviewed (via GET /cart/{session_id}, i.e. view_cart) before this
# checkout attempt -- not just an MCP tool docstring convention. In
# memory like _CARTS above; cleared after every checkout attempt (see
# clear_cart_reviewed) so it can't be reused without a fresh review.
_CART_REVIEWED: dict[str, bool] = {}


def mark_cart_reviewed(session_id: str):
    _CART_REVIEWED[session_id] = True


def was_cart_reviewed(session_id: str) -> bool:
    return _CART_REVIEWED.get(session_id, False)


def clear_cart_reviewed(session_id: str):
    _CART_REVIEWED[session_id] = False
