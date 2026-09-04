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

Persisted in SQLite (orders.db, alongside audit_trail.db) rather than
an in-memory dict, so a backend restart doesn't lose in-flight orders.

Stock is only ever touched by capture_order() below -- never by
create_order() -- so a payment that's created but never captured
never touches inventory. See catalog.decrement_stock / restore_stock.
capture_order() is guarded by a module-level lock: it spans a SQL
status check, several in-memory catalog.decrement_stock() calls, and
a possible rollback, which must all happen as one atomic step even
under concurrent capture attempts (e.g. a duplicate webhook delivery
racing a self-capture).

Also backs checkout idempotency: the same (session_id, idempotency_key)
pair always resolves to the same order, so a retried/duplicated
checkout call returns the original result instead of creating a
second order.
"""

import datetime
import json
import os
import sqlite3
import threading
import time
import uuid

from . import catalog

DB_PATH = os.path.join(os.path.dirname(__file__), "orders.db")

_CAPTURE_LOCK = threading.Lock()


def _get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS orders (
            order_id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            actor TEXT NOT NULL,
            line_items TEXT NOT NULL,      -- JSON list
            total_inr REAL NOT NULL,
            idempotency_key TEXT NOT NULL,
            payment_link_id TEXT,
            razorpay_order_id TEXT,
            response TEXT NOT NULL,        -- JSON
            status TEXT NOT NULL,          -- 'created' | 'captured' | 'capture_failed' | 'refunded'
            payment_id TEXT,               -- set at capture time, needed to issue a refund
            created_at REAL NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS idempotency (
            session_id TEXT NOT NULL,
            idempotency_key TEXT NOT NULL,
            order_id TEXT NOT NULL,
            PRIMARY KEY (session_id, idempotency_key)
        )
        """
    )
    return conn


def _row_to_order(row) -> dict:
    (order_id, session_id, actor, line_items_json, total_inr, idempotency_key,
     payment_link_id, razorpay_order_id, response_json, status, payment_id, created_at) = row
    return {
        "order_id": order_id,
        "session_id": session_id,
        "actor": actor,
        "line_items": json.loads(line_items_json),
        "total_inr": total_inr,
        "idempotency_key": idempotency_key,
        "payment_link_id": payment_link_id,
        "razorpay_order_id": razorpay_order_id,
        "response": json.loads(response_json),
        "status": status,
        "payment_id": payment_id,
        "created_at": created_at,
    }


_ORDER_COLUMNS = ("order_id, session_id, actor, line_items, total_inr, idempotency_key, "
                  "payment_link_id, razorpay_order_id, response, status, payment_id, created_at")


def new_order_id() -> str:
    return f"order_{uuid.uuid4().hex[:12]}"


def create_order(order_id: str, session_id: str, actor: str, line_items: list[dict],
                  total_inr: float, idempotency_key: str, response: dict,
                  payment_link_id: str | None = None, razorpay_order_id: str | None = None):
    conn = _get_conn()
    try:
        conn.execute(
            f"INSERT INTO orders ({_ORDER_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (order_id, session_id, actor, json.dumps([dict(li) for li in line_items]), total_inr,
             idempotency_key, payment_link_id, razorpay_order_id, json.dumps(response),
             "created", None, time.time()),
        )
        conn.execute(
            "INSERT OR IGNORE INTO idempotency (session_id, idempotency_key, order_id) VALUES (?, ?, ?)",
            (session_id, idempotency_key, order_id),
        )
        conn.commit()
    finally:
        conn.close()


def update_response(order_id: str, response: dict):
    """Re-persists the order's stored `response` (what an idempotent
    replay returns) -- needed whenever a caller computes the response
    BEFORE a later step (e.g. self-capture) changes it, such as
    /agent/pay setting response["status"] to its final value only after
    capture completes."""
    conn = _get_conn()
    try:
        conn.execute("UPDATE orders SET response = ? WHERE order_id = ?", (json.dumps(response), order_id))
        conn.commit()
    finally:
        conn.close()


def find_existing_order(session_id: str, idempotency_key: str) -> dict | None:
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT order_id FROM idempotency WHERE session_id = ? AND idempotency_key = ?",
            (session_id, idempotency_key),
        ).fetchone()
    finally:
        conn.close()
    return get_order(row[0]) if row else None


def find_by_payment_link(payment_link_id: str) -> dict | None:
    conn = _get_conn()
    try:
        row = conn.execute(
            f"SELECT {_ORDER_COLUMNS} FROM orders WHERE payment_link_id = ?", (payment_link_id,)
        ).fetchone()
    finally:
        conn.close()
    return _row_to_order(row) if row else None


def find_by_razorpay_order_id(razorpay_order_id: str) -> dict | None:
    conn = _get_conn()
    try:
        row = conn.execute(
            f"SELECT {_ORDER_COLUMNS} FROM orders WHERE razorpay_order_id = ?", (razorpay_order_id,)
        ).fetchone()
    finally:
        conn.close()
    return _row_to_order(row) if row else None


def get_order(order_id: str) -> dict | None:
    conn = _get_conn()
    try:
        row = conn.execute(f"SELECT {_ORDER_COLUMNS} FROM orders WHERE order_id = ?", (order_id,)).fetchone()
    finally:
        conn.close()
    return _row_to_order(row) if row else None


def _set_status(order_id: str, status: str, payment_id: str | None = None):
    conn = _get_conn()
    try:
        if payment_id is not None:
            conn.execute("UPDATE orders SET status = ?, payment_id = ? WHERE order_id = ?",
                          (status, payment_id, order_id))
        else:
            conn.execute("UPDATE orders SET status = ? WHERE order_id = ?", (status, order_id))
        conn.commit()
    finally:
        conn.close()


def capture_order(order_id: str, payment_id: str | None = None) -> tuple[bool, str | None]:
    """Decrements stock for every line item. If any line item no longer
    has enough stock (a race between order-creation time and capture
    time), rolls back whatever THIS attempt already decremented and
    marks the order capture_failed rather than partially fulfilling
    it -- an order is either fully captured or not captured at all.

    Guarded by _CAPTURE_LOCK so two concurrent capture attempts for the
    same (or different) orders -- e.g. a duplicate webhook delivery --
    can't interleave their stock-decrement/rollback steps."""
    with _CAPTURE_LOCK:
        order = get_order(order_id)
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
                _set_status(order_id, "capture_failed")
                return False, "insufficient_stock_at_capture"

        _set_status(order_id, "captured", payment_id=payment_id)
        return True, None


def refund_order(order_id: str) -> tuple[bool, str | None]:
    """Restores stock for every line item and marks the order refunded.
    Only a captured order can be refunded; refunding is idempotent --
    an already-refunded order just returns success without double-
    restoring stock."""
    with _CAPTURE_LOCK:
        order = get_order(order_id)
        if not order:
            return False, "order_not_found"
        if order["status"] == "refunded":
            return True, None
        if order["status"] != "captured":
            return False, "order_not_captured"

        for li in order["line_items"]:
            catalog.restore_stock(li["product_id"], li["qty"])
        _set_status(order_id, "refunded")
        return True, None


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
    conn = _get_conn()
    try:
        rows = conn.execute(
            "SELECT total_inr, created_at FROM orders WHERE actor = ? AND status = 'created'",
            (actor,),
        ).fetchall()
    finally:
        conn.close()
    return sum(
        total_inr for total_inr, created_at in rows
        if datetime.datetime.fromtimestamp(created_at).date() == today
    )
