"""
Growth metrics -- the quantified half of "AI Growth & Agentic Commerce".

Every number here is a read over data the rest of the system was
already writing for the "explainable" requirement (audit.py's SQLite
trail), plus one small in-memory counter in cart.py for upsell
acceptance. No new tables, no schema migration -- this module only
aggregates what already exists.
"""

import sqlite3

from . import audit, cart

ACTORS = ("human_whatsapp", "ai_agent_mcp")


def _query_scalar(sql: str, params: tuple = ()):
    conn = sqlite3.connect(audit.DB_PATH)
    try:
        return conn.execute(sql, params).fetchone()[0]
    finally:
        conn.close()


def _revenue_inr(actor: str | None = None) -> float:
    sql = "SELECT COALESCE(SUM(amount_inr), 0) FROM audit_log WHERE action = 'checkout_payment' AND status IN ('ok', 'retried')"
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

    return {
        "total_revenue_inr": _revenue_inr(),
        "revenue_by_actor": {a: _revenue_inr(a) for a in ACTORS},
        "checkout_conversion_rate": {
            "overall": _conversion_rate(),
            "by_actor": {a: _conversion_rate(a) for a in ACTORS},
        },
        "upsell_shown_count": upsell_shown_count,
        "upsell_accepted_count": upsell_accepted_count,
        "upsell_acceptance_rate": upsell_acceptance_rate,
    }
