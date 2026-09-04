"""
Order store -- created once a payment exists on ONE of the two rails
(checkout reached Razorpay successfully), captured (or capture_failed)
once a payment is actually confirmed (a real Razorpay webhook, or the
signed /demo/simulate-capture stand-in -- see webhooks.py, both run
through the exact same verification+capture code).

  - Human rail (POST /checkout): Payment Links API -- order carries a
    `payment_link_id`, `razorpay_order_id` is None.
  - Agent rail (POST /agent/pay): Orders API -- order carries a
    `razorpay_order_id`, `payment_link_id` is None.

Stock is only ever touched by capture_order() below -- never by
create_order() -- so a payment that's created but never captured
never touches inventory. See catalog.decrement_stock / restore_stock.

Also backs checkout idempotency: the same (session_id, idempotency_key)
pair always resolves to the same order, so a retried/duplicated
checkout call returns the original result instead of creating a
second order.
"""

import datetime
import time
import uuid

from . import catalog

_ORDERS: dict[str, dict] = {}
_IDEMPOTENCY: dict[tuple[str, str], str] = {}


def new_order_id() -> str:
    return f"order_{uuid.uuid4().hex[:12]}"


def create_order(order_id: str, session_id: str, actor: str, line_items: list[dict],
                  total_inr: float, idempotency_key: str, response: dict,
                  payment_link_id: str | None = None, razorpay_order_id: str | None = None):
    _ORDERS[order_id] = {
        "order_id": order_id,
        "session_id": session_id,
        "actor": actor,
        "line_items": [dict(li) for li in line_items],
        "total_inr": total_inr,
        "idempotency_key": idempotency_key,
        "payment_link_id": payment_link_id,
        "razorpay_order_id": razorpay_order_id,
        "response": response,
        "status": "created",
        "created_at": time.time(),
    }
    _IDEMPOTENCY[(session_id, idempotency_key)] = order_id


def find_existing_order(session_id: str, idempotency_key: str) -> dict | None:
    order_id = _IDEMPOTENCY.get((session_id, idempotency_key))
    return _ORDERS.get(order_id) if order_id else None


def find_by_payment_link(payment_link_id: str) -> dict | None:
    for order in _ORDERS.values():
        if order["payment_link_id"] == payment_link_id:
            return order
    return None


def find_by_razorpay_order_id(razorpay_order_id: str) -> dict | None:
    for order in _ORDERS.values():
        if order["razorpay_order_id"] == razorpay_order_id:
            return order
    return None


def get_order(order_id: str) -> dict | None:
    return _ORDERS.get(order_id)


def pending_spend_today(actor: str) -> float:
    """Sum of this actor's orders still sitting in 'created' status
    (payment initiated but not yet captured OR failed) from today --
    closes the gap where the daily cap only counted CAPTURED spend
    (see audit.captured_spend_today). In today's architecture
    POST /agent/pay always attempts self-capture synchronously in the
    same request, so an order only lingers in 'created' if the process
    crashed between creating it and capturing it -- a rare but real
    case this still needs to count against the cap, not silently drop
    out of it."""
    today = datetime.date.today()
    total = 0.0
    for order in _ORDERS.values():
        if order["actor"] != actor or order["status"] != "created":
            continue
        if datetime.datetime.fromtimestamp(order["created_at"]).date() == today:
            total += order["total_inr"]
    return total


def capture_order(order_id: str) -> tuple[bool, str | None]:
    """Decrements stock for every line item. If any line item no longer
    has enough stock (a race between order-creation time and capture
    time), rolls back whatever THIS attempt already decremented and
    marks the order capture_failed rather than partially fulfilling
    it -- an order is either fully captured or not captured at all."""
    order = _ORDERS.get(order_id)
    if not order:
        return False, "order_not_found"
    if order["status"] == "captured":
        return True, None  # idempotent -- a webhook can legitimately fire twice

    decremented = []
    for li in order["line_items"]:
        if catalog.decrement_stock(li["product_id"], li["qty"]):
            decremented.append((li["product_id"], li["qty"]))
        else:
            for pid, qty in decremented:
                catalog.restore_stock(pid, qty)
            order["status"] = "capture_failed"
            return False, "insufficient_stock_at_capture"

    order["status"] = "captured"
    return True, None
