"""
Cart store, keyed by session id -- backed by db.py's shared SQLModel
database, not an in-memory dict, so a backend restart doesn't silently
empty every buyer's in-progress cart.

Every row carries merchant_id directly (not just derivable via a join
through session_id), so a query can filter on merchant_id alone
without a join, and the tenant boundary is explicit even if a future
caller queries these tables directly.

Both the WhatsApp flow and the MCP flow call into this same module, so
there is one cart implementation, not two -- avoids the classic demo
bug of the "AI buyer" and "human buyer" secretly running on different
logic.
"""

import time

from sqlmodel import select

from . import catalog, db


def get_cart(session_id: str) -> list[dict]:
    with db.get_session() as s:
        rows = s.exec(
            select(db.CartItem).where(db.CartItem.session_id == session_id)
        ).all()
    return [{"product_id": r.product_id, "name": r.name, "qty": r.qty, "price_inr": r.price_inr} for r in rows]


def add_to_cart(merchant_id: str, session_id: str, product_id: str, qty: int = 1):
    product = catalog.get_product(merchant_id, product_id)
    if not product:
        return None, "product_not_found"

    with db.get_session() as s:
        existing = s.get(db.CartItem, (session_id, product_id))
        if existing:
            existing.qty += qty
            s.add(existing)
        else:
            s.add(db.CartItem(session_id=session_id, product_id=product_id, merchant_id=merchant_id,
                               name=product["name"], qty=qty, price_inr=product["price_inr"]))

    # Any mutation invalidates a prior review -- "cart_reviewed since
    # last mutation" (policy.py rule 7) means exactly that: adding
    # another item after a GET /cart/{session_id} review, without
    # reviewing again, must NOT still count as reviewed.
    clear_cart_reviewed(session_id, merchant_id)
    return get_cart(session_id), None


def remove_from_cart(session_id: str, product_id: str):
    """Removes a line item entirely (not a partial-quantity decrement --
    kept to that one, simple, demoable semantic). Returns (cart, None)
    on success, (cart, "product_not_in_cart") if there was nothing to
    remove -- the caller decides whether that's an error worth
    surfacing."""
    with db.get_session() as s:
        existing = s.get(db.CartItem, (session_id, product_id))
        if not existing:
            return get_cart(session_id), "product_not_in_cart"
        merchant_id = existing.merchant_id
        s.delete(existing)

    clear_cart_reviewed(session_id, merchant_id)  # a removal is a mutation too
    return get_cart(session_id), None


def set_line_item_price_for_tests(session_id: str, product_id: str, price_inr: float):
    """Test-only helper -- overwrites a cart line's stored price. Normal
    add_to_cart() never lets a client set this value; this exists so a
    test can simulate a tampered/corrupted cart entry to prove
    policy.py's price-tamper check (which recomputes from the live
    server catalog, ignoring this value) actually works."""
    with db.get_session() as s:
        item = s.get(db.CartItem, (session_id, product_id))
        if item:
            item.price_inr = price_inr
            s.add(item)


def cart_total(session_id: str) -> float:
    cart = get_cart(session_id)
    return sum(line["qty"] * line["price_inr"] for line in cart)


def clear_cart(session_id: str):
    with db.get_session() as s:
        rows = s.exec(select(db.CartItem).where(db.CartItem.session_id == session_id)).all()
        for row in rows:
            s.delete(row)


def record_upsell_suggested(session_id: str, merchant_id: str, product_id: str):
    with db.get_session() as s:
        existing = s.get(db.SuggestedUpsell, (session_id, product_id))
        if not existing:
            s.add(db.SuggestedUpsell(session_id=session_id, product_id=product_id, merchant_id=merchant_id))


def check_and_record_upsell_acceptance(session_id: str, merchant_id: str, product_id: str) -> bool:
    """If product_id was previously suggested as an upsell for this
    session, counts this add as an accepted upsell and returns True."""
    with db.get_session() as s:
        was_suggested = s.get(db.SuggestedUpsell, (session_id, product_id))
        if was_suggested:
            s.add(db.UpsellAcceptance(session_id=session_id, merchant_id=merchant_id,
                                       product_id=product_id, created_at=time.time()))
            return True
        return False


def get_upsell_accepted_count(merchant_id: str | None = None) -> int:
    with db.get_session() as s:
        query = select(db.UpsellAcceptance)
        if merchant_id is not None:
            query = query.where(db.UpsellAcceptance.merchant_id == merchant_id)
        return len(s.exec(query).all())


def mark_cart_reviewed(session_id: str, merchant_id: str):
    with db.get_session() as s:
        row = s.get(db.CartReviewed, session_id)
        if row:
            row.reviewed = True
            s.add(row)
        else:
            s.add(db.CartReviewed(session_id=session_id, merchant_id=merchant_id, reviewed=True))


def was_cart_reviewed(session_id: str) -> bool:
    with db.get_session() as s:
        row = s.get(db.CartReviewed, session_id)
        return bool(row and row.reviewed)


def clear_cart_reviewed(session_id: str, merchant_id: str):
    with db.get_session() as s:
        row = s.get(db.CartReviewed, session_id)
        if row:
            row.reviewed = False
            s.add(row)
        else:
            s.add(db.CartReviewed(session_id=session_id, merchant_id=merchant_id, reviewed=False))
