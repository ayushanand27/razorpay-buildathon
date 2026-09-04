"""
Cart store, keyed by session id -- persisted in SQLite (carts.db,
alongside audit_trail.db) rather than an in-memory dict, so a backend
restart doesn't silently empty every buyer's in-progress cart.

Both the WhatsApp flow and the MCP flow call into this same module, so
there is one cart implementation, not two -- avoids the classic demo
bug of the "AI buyer" and "human buyer" secretly running on different
logic.
"""

import os
import sqlite3
import time

from .catalog import get_product

DB_PATH = os.path.join(os.path.dirname(__file__), "carts.db")


def _get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS cart_items (
            session_id TEXT NOT NULL,
            product_id TEXT NOT NULL,
            name TEXT NOT NULL,
            qty INTEGER NOT NULL,
            price_inr REAL NOT NULL,
            PRIMARY KEY (session_id, product_id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS cart_reviewed (
            session_id TEXT PRIMARY KEY,
            reviewed INTEGER NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS suggested_upsells (
            session_id TEXT NOT NULL,
            product_id TEXT NOT NULL,
            PRIMARY KEY (session_id, product_id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS upsell_acceptances (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            product_id TEXT NOT NULL,
            created_at REAL NOT NULL
        )
        """
    )
    return conn


def get_cart(session_id: str) -> list[dict]:
    conn = _get_conn()
    try:
        rows = conn.execute(
            "SELECT product_id, name, qty, price_inr FROM cart_items WHERE session_id = ? ORDER BY rowid",
            (session_id,),
        ).fetchall()
    finally:
        conn.close()
    return [{"product_id": r[0], "name": r[1], "qty": r[2], "price_inr": r[3]} for r in rows]


def add_to_cart(session_id: str, product_id: str, qty: int = 1):
    product = get_product(product_id)
    if not product:
        return None, "product_not_found"

    conn = _get_conn()
    try:
        existing = conn.execute(
            "SELECT qty FROM cart_items WHERE session_id = ? AND product_id = ?",
            (session_id, product_id),
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE cart_items SET qty = qty + ? WHERE session_id = ? AND product_id = ?",
                (qty, session_id, product_id),
            )
        else:
            conn.execute(
                "INSERT INTO cart_items (session_id, product_id, name, qty, price_inr) VALUES (?, ?, ?, ?, ?)",
                (session_id, product_id, product["name"], qty, product["price_inr"]),
            )
        conn.commit()
    finally:
        conn.close()

    # Any mutation invalidates a prior review -- "cart_reviewed since
    # last mutation" (policy.py rule 7) means exactly that: adding
    # another item after a GET /cart/{session_id} review, without
    # reviewing again, must NOT still count as reviewed.
    clear_cart_reviewed(session_id)
    return get_cart(session_id), None


def remove_from_cart(session_id: str, product_id: str):
    """Removes a line item entirely (not a partial-quantity decrement --
    kept to that one, simple, demoable semantic). Returns (cart, None)
    on success, (cart, "product_not_in_cart") if there was nothing to
    remove -- the caller decides whether that's an error worth
    surfacing."""
    conn = _get_conn()
    try:
        cur = conn.execute(
            "DELETE FROM cart_items WHERE session_id = ? AND product_id = ?",
            (session_id, product_id),
        )
        conn.commit()
        removed = cur.rowcount > 0
    finally:
        conn.close()

    if removed:
        clear_cart_reviewed(session_id)  # a removal is a mutation too
        return get_cart(session_id), None
    return get_cart(session_id), "product_not_in_cart"


def set_line_item_price_for_tests(session_id: str, product_id: str, price_inr: float):
    """Test-only helper -- overwrites a cart line's stored price. Normal
    add_to_cart() never lets a client set this value; this exists so a
    test can simulate a tampered/corrupted cart entry to prove
    policy.py's price-tamper check (which recomputes from the live
    server catalog, ignoring this value) actually works."""
    conn = _get_conn()
    try:
        conn.execute(
            "UPDATE cart_items SET price_inr = ? WHERE session_id = ? AND product_id = ?",
            (price_inr, session_id, product_id),
        )
        conn.commit()
    finally:
        conn.close()


def cart_total(session_id: str) -> float:
    cart = get_cart(session_id)
    return sum(line["qty"] * line["price_inr"] for line in cart)


def clear_cart(session_id: str):
    conn = _get_conn()
    try:
        conn.execute("DELETE FROM cart_items WHERE session_id = ?", (session_id,))
        conn.commit()
    finally:
        conn.close()


def record_upsell_suggested(session_id: str, product_id: str):
    conn = _get_conn()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO suggested_upsells (session_id, product_id) VALUES (?, ?)",
            (session_id, product_id),
        )
        conn.commit()
    finally:
        conn.close()


def check_and_record_upsell_acceptance(session_id: str, product_id: str) -> bool:
    """If product_id was previously suggested as an upsell for this
    session, counts this add as an accepted upsell and returns True."""
    conn = _get_conn()
    try:
        was_suggested = conn.execute(
            "SELECT 1 FROM suggested_upsells WHERE session_id = ? AND product_id = ?",
            (session_id, product_id),
        ).fetchone()
        if was_suggested:
            conn.execute(
                "INSERT INTO upsell_acceptances (session_id, product_id, created_at) VALUES (?, ?, ?)",
                (session_id, product_id, time.time()),
            )
            conn.commit()
            return True
        return False
    finally:
        conn.close()


def get_upsell_accepted_count() -> int:
    conn = _get_conn()
    try:
        return conn.execute("SELECT COUNT(*) FROM upsell_acceptances").fetchone()[0]
    finally:
        conn.close()


def mark_cart_reviewed(session_id: str):
    conn = _get_conn()
    try:
        conn.execute(
            "INSERT INTO cart_reviewed (session_id, reviewed) VALUES (?, 1) "
            "ON CONFLICT(session_id) DO UPDATE SET reviewed = 1",
            (session_id,),
        )
        conn.commit()
    finally:
        conn.close()


def was_cart_reviewed(session_id: str) -> bool:
    conn = _get_conn()
    try:
        row = conn.execute("SELECT reviewed FROM cart_reviewed WHERE session_id = ?", (session_id,)).fetchone()
    finally:
        conn.close()
    return bool(row and row[0])


def clear_cart_reviewed(session_id: str):
    conn = _get_conn()
    try:
        conn.execute(
            "INSERT INTO cart_reviewed (session_id, reviewed) VALUES (?, 0) "
            "ON CONFLICT(session_id) DO UPDATE SET reviewed = 0",
            (session_id,),
        )
        conn.commit()
    finally:
        conn.close()
