"""
Growth metrics -- the quantified half of "AI Growth & Agentic Commerce".

Revenue is CAPTURED money only (payment_confirmed, status=paid) -- a
payment link being created is an order, not revenue yet. See
orders_created_inr for the "link created but not yet (or never) paid"
number, so the dashboard can show the gap between the two instead of
conflating them.

Every number here is a read over data the rest of the system was
already writing for the "explainable" requirement (audit.py's SQLite
trail), plus one small in-memory counter in cart.py for upsell
acceptance. No new tables, no schema migration.
"""

import json
import sqlite3

from . import audit, cart

ACTORS = ("human_whatsapp", "ai_agent_mcp")


def _query_scalar(sql: str, params: tuple = ()):
    conn = sqlite3.connect(audit.DB_PATH)
    try:
        # Same schema as audit.py's _get_conn() -- /metrics can be the
        # very first request against a brand-new audit_trail.db, before
        # any log_action() call has created the table.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL,
                actor TEXT NOT NULL,          -- 'human_whatsapp' | 'ai_agent_mcp'
                actor_id TEXT,                -- phone number / mcp session id
                action TEXT NOT NULL,         -- e.g. 'checkout_attempt', 'checkout_success'
                amount_inr REAL,
                status TEXT NOT NULL,         -- 'ok' | 'blocked' | 'failed' | 'retried'
                details TEXT                  -- JSON blob, free-form
            )
            """
        )
        return conn.execute(sql, params).fetchone()[0]
    finally:
        conn.close()


def _captured_inr(actor: str | None = None) -> float:
    """CAPTURED revenue only -- payment_confirmed, status=paid. This is
    the number that actually moved money; a checkout_payment row alone
    (a payment link being created) is not proof of that."""
    sql = "SELECT COALESCE(SUM(amount_inr), 0.0) FROM audit_log WHERE action = 'payment_confirmed' AND status = 'paid'"
    params: tuple = ()
    if actor:
        sql += " AND actor = ?"
        params = (actor,)
    return _query_scalar(sql, params)


def _orders_created_inr(actor: str | None = None) -> float:
    """Payment LINKS created (ok + retried) -- may include links never
    actually paid. Compared against _captured_inr(), this is the
    "created but not yet captured" gap the dashboard shows."""
    sql = "SELECT COALESCE(SUM(amount_inr), 0.0) FROM audit_log WHERE action = 'checkout_payment' AND status IN ('ok', 'retried')"
    params: tuple = ()
    if actor:
        sql += " AND actor = ?"
        params = (actor,)
    return _query_scalar(sql, params)


def _count(action: str, statuses: tuple[str, ...], actor: str | None = None) -> int:
    placeholders = ",".join("?" * len(statuses))
    sql = f"SELECT COUNT(*) FROM audit_log WHERE action = ? AND status IN ({placeholders})"
    params = [action, *statuses]
    if actor:
        sql += " AND actor = ?"
        params.append(actor)
    return _query_scalar(sql, tuple(params))


def _upsell_blocked_by_cap_count() -> int:
    """Counts upsell_blocked entries specifically for reason ==
    would_exceed_cap -- the other two blocked reasons (oos,
    already_in_cart) aren't spending-cap events, so they're excluded
    from this specific counter. Filters in Python rather than via
    SQLite's json_extract() to stay portable across sqlite3 builds
    without a JSON1 dependency; the audit trail is small enough that
    this costs nothing in practice."""
    conn = sqlite3.connect(audit.DB_PATH)
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL,
                actor TEXT NOT NULL,
                actor_id TEXT,
                action TEXT NOT NULL,
                amount_inr REAL,
                status TEXT NOT NULL,
                details TEXT
            )
            """
        )
        rows = conn.execute(
            "SELECT details FROM audit_log WHERE action = 'upsell_blocked' AND status = 'blocked'"
        ).fetchall()
    finally:
        conn.close()

    count = 0
    for (details_json,) in rows:
        try:
            if json.loads(details_json or "{}").get("reason") == "would_exceed_cap":
                count += 1
        except (TypeError, ValueError):
            pass
    return count


def _conversion_rate(actor: str | None = None) -> float:
    attempts = _count("checkout_attempt", ("ok",), actor)
    payments = _count("checkout_payment", ("ok", "retried"), actor)
    if attempts == 0:
        return 0.0
    return round(payments / attempts * 100, 1)


def get_metrics():
    upsell_shown_count = _count("upsell_shown", ("ok",))
    upsell_accepted_count = cart.get_upsell_accepted_count()
    upsell_acceptance_rate = (
        round(upsell_accepted_count / upsell_shown_count * 100, 1) if upsell_shown_count else 0.0
    )

    captured = _captured_inr()
    orders_created = _orders_created_inr()

    return {
        "total_revenue_inr": captured,
        "captured_inr": captured,
        "orders_created_inr": orders_created,
        "revenue_by_actor": {a: _captured_inr(a) for a in ACTORS},
        "orders_created_by_actor": {a: _orders_created_inr(a) for a in ACTORS},
        "checkout_conversion_rate": {
            "overall": _conversion_rate(),
            "by_actor": {a: _conversion_rate(a) for a in ACTORS},
        },
        "upsell_shown_count": upsell_shown_count,
        "upsell_accepted_count": upsell_accepted_count,
        "upsell_acceptance_rate": upsell_acceptance_rate,
        "upsell_blocked_by_cap_count": _upsell_blocked_by_cap_count(),
    }
