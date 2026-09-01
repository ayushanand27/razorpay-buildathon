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
