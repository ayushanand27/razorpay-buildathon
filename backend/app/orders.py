"""
Order store -- db.Order, part of the single shared app.db (not a
separate orders.db anymore). Created once a payment exists on ONE of
the two rails (checkout reached Razorpay successfully), captured (or
capture_failed) once a payment is actually confirmed (a real Razorpay
webhook, or the signed /demo/simulate-capture stand-in -- see
webhooks.py, both run through the exact same verification+capture
code).

  - Human rail (POST /checkout): Payment Links API -- order carries a
    `payment_link_id`, `razorpay_order_id` is None.
  - Agent rail (POST /agent/pay): Orders API -- order carries a
    `razorpay_order_id`, `payment_link_id` is None.

Idempotency is now a real database-level guarantee, not an in-memory
lock: claim_idempotency_key()/find_existing_order() below delegate to
db.claim_idempotency_key()/db.find_order_for_idempotency_key(), which
rely on a PRIMARY KEY constraint that SQLite enforces atomically even
across separate worker processes -- see db.py's IdempotencyRecord
docstring. main.py claims the key BEFORE calling out to Razorpay; if
the claim is lost (a concurrent duplicate of the same request), it
waits briefly for the winning request's order to become visible and
replays that stored response instead of creating a second order.

Stock is only ever touched by capture_order() below -- never by
create_order() -- so a payment that's created but never captured never
touches inventory. capture_order()/refund_order() replace the old
in-memory threading.Lock() with an atomic conditional UPDATE (`WHERE
status = '<expected>'`): SQLite serializes all writes to one database
file globally, so a status transition guarded by a WHERE clause and a
rowcount check is a real database-level claim, not a same-process-only
guard -- it holds even under a multi-worker deployment, which a
threading.Lock() dict never could.
"""

import datetime
import json
import uuid

from sqlmodel import select
from sqlalchemy import update as sa_update

from . import catalog, db


def new_order_id() -> str:
    return f"order_{uuid.uuid4().hex[:12]}"


def _order_to_dict(o: "db.Order") -> dict:
    return {
        "order_id": o.order_id,
        "merchant_id": o.merchant_id,
        "session_id": o.session_id,
        "actor": o.actor,
        "line_items": json.loads(o.line_items_json),
        "total_inr": o.total_inr,
        "idempotency_key": o.idempotency_key,
        "payment_link_id": o.payment_link_id,
        "razorpay_order_id": o.razorpay_order_id,
        "response": db.json_loads(o.response_json),
        "status": o.status,
        "payment_id": o.payment_id,
        "created_at": o.created_at,
    }


def create_order(order_id: str, merchant_id: str, session_id: str, actor: str, line_items: list[dict],
                  total_inr: float, idempotency_key: str, response: dict,
                  payment_link_id: str | None = None, razorpay_order_id: str | None = None,
                  session=None):
    """Inserts the Order row. Pass an open `session` (a `with
    db.get_session() as s:` block) to make this insert commit-or-
    rollback together with a caller-added audit.log_action(..., session=s)
    call, the ATOMIC "order creation + its audit entry" guarantee the
    blueprint calls for -- see main.py's /checkout and /agent/pay.
    Omit `session` to commit standalone (existing test call sites)."""
    order = db.Order(
        order_id=order_id, merchant_id=merchant_id, session_id=session_id, actor=actor,
        line_items_json=json.dumps([dict(li) for li in line_items]),
        total_inr=total_inr, idempotency_key=idempotency_key,
        payment_link_id=payment_link_id, razorpay_order_id=razorpay_order_id,
        response_json=json.dumps(response), status="created",
    )
    if session is not None:
        session.add(order)
        session.flush()
        return
    with db.get_session() as s:
        s.add(order)


def update_response(order_id: str, response: dict):
    """Re-persists the order's stored `response` (what an idempotent
    replay returns) -- needed whenever a caller computes the response
    BEFORE a later step (e.g. self-capture) changes it, such as
    /agent/pay setting response["status"] to its final value only after
    capture completes."""
    with db.get_session() as s:
        order = s.get(db.Order, order_id)
        if order:
            order.response_json = json.dumps(response)
            s.add(order)


def claim_idempotency_key(session_id: str, idempotency_key: str, order_id: str) -> bool:
    """True if THIS caller now owns (session_id, idempotency_key) and
    should proceed to create order_id; False if a concurrent request
    (same or a different worker process) already claimed it first --
    see db.claim_idempotency_key()."""
    return db.claim_idempotency_key(session_id, idempotency_key, order_id)


def release_idempotency_key(session_id: str, idempotency_key: str):
    """Releases a claim that never resulted in a created order (blocked
    by a guardrail/policy check, or the payment call itself failed) so
    the SAME (session_id, idempotency_key) can be retried instead of
    being permanently burned by a failed attempt -- see
    db.release_idempotency_key()."""
    db.release_idempotency_key(session_id, idempotency_key)


def find_existing_order(session_id: str, idempotency_key: str, wait_seconds: float = 2.0) -> dict | None:
    """Looks up the order a prior claim of (session_id, idempotency_key)
    points at. wait_seconds > 0 briefly retries so a request that just
    lost a claim race to a still-in-flight concurrent request (its
    Order row not committed yet) gets that request's real result
    instead of a false cart_empty/not_found."""
    order = db.find_order_for_idempotency_key(session_id, idempotency_key, wait_seconds=wait_seconds)
    return _order_to_dict(order) if order else None


def find_by_payment_link(payment_link_id: str) -> dict | None:
    with db.get_session() as s:
        order = s.exec(select(db.Order).where(db.Order.payment_link_id == payment_link_id)).first()
        return _order_to_dict(order) if order else None


def find_by_razorpay_order_id(razorpay_order_id: str) -> dict | None:
    with db.get_session() as s:
        order = s.exec(select(db.Order).where(db.Order.razorpay_order_id == razorpay_order_id)).first()
        return _order_to_dict(order) if order else None


def get_order(order_id: str) -> dict | None:
    with db.get_session() as s:
        order = s.get(db.Order, order_id)
        return _order_to_dict(order) if order else None


def _set_status(order_id: str, status: str, payment_id: str | None = None):
    with db.get_session() as s:
        order = s.get(db.Order, order_id)
        if order:
            order.status = status
            if payment_id is not None:
                order.payment_id = payment_id
            s.add(order)


def capture_order(order_id: str, payment_id: str | None = None) -> tuple[bool, str | None]:
    """Atomically claims the transition created -> capturing via a
    conditional UPDATE ... WHERE status = 'created' (rowcount == 1 means
    THIS call won the claim) before touching stock or anything else --
    SQLite serializes all writes to one database file, so this WHERE-
    guarded UPDATE is a real cross-process claim, replacing the old
    in-memory threading.Lock() that only ever protected one process.

    Once claimed, decrements stock for every line item. If any line
    item no longer has enough stock (a race between order-creation
    time and capture time), rolls back whatever THIS attempt already
    decremented and marks the order capture_failed rather than
    partially fulfilling it -- an order is either fully captured or
    not captured at all."""
    with db.get_session() as s:
        result = s.execute(
            sa_update(db.Order)
            .where(db.Order.order_id == order_id, db.Order.status == "created")
            .values(status="capturing")
        )
        claimed = result.rowcount == 1

    if not claimed:
        order = get_order(order_id)
        if not order:
            return False, "order_not_found"
        if order["status"] == "captured":
            return True, "already_captured"  # idempotent -- a webhook can legitimately fire twice
        if order["status"] == "capturing":
            return False, "capture_in_progress_elsewhere"
        return False, f"order_not_capturable: status={order['status']}"

    order = get_order(order_id)
    decremented = []
    for li in order["line_items"]:
        if catalog.decrement_stock(order["merchant_id"], li["product_id"], li["qty"]):
            decremented.append((li["product_id"], li["qty"]))
        else:
            for pid, qty in decremented:
                catalog.restore_stock(order["merchant_id"], pid, qty)
            _set_status(order_id, "capture_failed")
            return False, "insufficient_stock_at_capture"

    _set_status(order_id, "captured", payment_id=payment_id)
    return True, None


def refund_order(order_id: str) -> tuple[bool, str | None]:
    """Same atomic-claim pattern as capture_order(): the captured ->
    refunded transition is claimed via a conditional UPDATE, not an
    in-memory lock. Only a captured order can be refunded; refunding is
    idempotent -- an already-refunded order just returns success
    without double-restoring stock."""
    with db.get_session() as s:
        result = s.execute(
            sa_update(db.Order)
            .where(db.Order.order_id == order_id, db.Order.status == "captured")
            .values(status="refunded")
        )
        claimed = result.rowcount == 1

    if not claimed:
        order = get_order(order_id)
        if not order:
            return False, "order_not_found"
        if order["status"] == "refunded":
            return True, None
        return False, "order_not_captured"

    order = get_order(order_id)
    for li in order["line_items"]:
        catalog.restore_stock(order["merchant_id"], li["product_id"], li["qty"])
    return True, None


def pending_spend_today(merchant_id: str, actor: str) -> float:
    """Sum of this actor's orders AT THIS MERCHANT still sitting in
    'created' or 'capturing' status (payment initiated but not yet
    captured OR failed) from today -- closes the gap where the daily
    cap only counted CAPTURED spend (see audit.captured_spend_today).
    Scoped by merchant_id because a warrant's daily cap is itself
    per-(agent, merchant): spend pending at one merchant must never
    affect a cap at another."""
    today = datetime.date.today()
    with db.get_session() as s:
        rows = s.exec(
            select(db.Order.total_inr, db.Order.created_at)
            .where(db.Order.merchant_id == merchant_id, db.Order.actor == actor,
                   db.Order.status.in_(("created", "capturing")))
        ).all()
    return sum(
        total_inr for total_inr, created_at in rows
        if datetime.datetime.fromtimestamp(created_at).date() == today
    )
