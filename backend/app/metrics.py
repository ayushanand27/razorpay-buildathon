"""
Growth metrics -- the quantified half of "AI Growth & Agentic Commerce".

Revenue is CAPTURED money only (payment_confirmed, status=paid) -- a
payment link being created is an order, not revenue yet. See
orders_created_inr for the "link created but not yet (or never) paid"
number, so the dashboard can show the gap between the two instead of
conflating them.

Every number here is a read over db.AuditLog (the same single app.db
everything else uses), plus one small counter in cart.py's
upsell_acceptances table. merchant_id is a real column on AuditLog now,
so scoping is a direct WHERE filter -- no more resolving each row's
actor_id back to a session to find its merchant.

merchant_id=None here means "aggregate across every merchant" -- an
intentional platform-wide total (GET /metrics), distinct from the
audit trail itself: this endpoint only ever returns aggregated counts
and sums, never the underlying per-tenant rows, so it doesn't reopen
the raw cross-tenant data leak that GET /audit-trail was purged of
(see audit.py). GET /merchants/{merchant_id}/metrics passes a real
merchant_id for one merchant's own numbers.
"""

import json

from sqlmodel import select

from . import cart, db

ACTORS = ("human_whatsapp", "ai_agent_mcp")


def _rows(action: str, statuses: tuple[str, ...], actor: str | None = None,
          merchant_id: str | None = None) -> list[tuple]:
    """Returns (actor_id, amount_inr, details) rows matching action/statuses
    (and actor/merchant_id, if given) -- the raw material every
    aggregate below is computed from."""
    with db.get_session() as s:
        query = select(db.AuditLog.actor_id, db.AuditLog.amount_inr, db.AuditLog.details).where(
            db.AuditLog.action == action, db.AuditLog.status.in_(statuses)
        )
        if actor:
            query = query.where(db.AuditLog.actor == actor)
        if merchant_id is not None:
            query = query.where(db.AuditLog.merchant_id == merchant_id)
        return s.exec(query).all()


def _sum_amount(action: str, statuses: tuple[str, ...], actor: str | None = None,
                 merchant_id: str | None = None) -> float:
    rows = _rows(action, statuses, actor, merchant_id)
    return sum(r[1] or 0.0 for r in rows)


def _count(action: str, statuses: tuple[str, ...], actor: str | None = None,
           merchant_id: str | None = None) -> int:
    return len(_rows(action, statuses, actor, merchant_id))


def _captured_inr(actor: str | None = None, merchant_id: str | None = None) -> float:
    """CAPTURED revenue only -- payment_confirmed, status=paid. This is
    the number that actually moved money; a checkout_payment row alone
    (a payment link being created) is not proof of that."""
    return _sum_amount("payment_confirmed", ("paid",), actor, merchant_id)


def _orders_created_inr(actor: str | None = None, merchant_id: str | None = None) -> float:
    """Payment LINKS/orders created (ok + retried) -- may include ones
    never actually paid. Compared against _captured_inr(), this is the
    "created but not yet captured" gap the dashboard shows."""
    return _sum_amount("checkout_payment", ("ok", "retried"), actor, merchant_id)


def _refunded_inr(actor: str | None = None, merchant_id: str | None = None) -> float:
    """Sum of successfully refunded orders (action='refund', status='ok')
    -- captured_inr/total_revenue_inr are left as the GROSS captured
    figure (unchanged meaning, for backward compatibility); this is a
    separate figure so a refund shows up honestly rather than silently
    vanishing from either number."""
    return _sum_amount("refund", ("ok",), actor, merchant_id)


def _upsell_blocked_by_cap_count(merchant_id: str | None = None) -> int:
    """Counts upsell_blocked entries specifically for reason ==
    would_exceed_cap -- the other two blocked reasons (oos,
    already_in_cart) aren't spending-cap events, so they're excluded
    from this specific counter."""
    rows = _rows("upsell_blocked", ("blocked",), merchant_id=merchant_id)
    count = 0
    for (_actor_id, _amount, details_json) in rows:
        try:
            if json.loads(details_json or "{}").get("reason") == "would_exceed_cap":
                count += 1
        except (TypeError, ValueError):
            pass
    return count


def _conversion_rate(actor: str | None = None, merchant_id: str | None = None) -> float:
    attempts = _count("checkout_attempt", ("ok",), actor, merchant_id)
    payments = _count("checkout_payment", ("ok", "retried"), actor, merchant_id)
    if attempts == 0:
        return 0.0
    return round(payments / attempts * 100, 1)


def get_metrics(merchant_id: str | None = None):
    upsell_shown_count = _count("upsell_shown", ("ok",), merchant_id=merchant_id)
    upsell_accepted_count = cart.get_upsell_accepted_count(merchant_id)
    upsell_acceptance_rate = (
        round(upsell_accepted_count / upsell_shown_count * 100, 1) if upsell_shown_count else 0.0
    )

    captured = _captured_inr(merchant_id=merchant_id)
    orders_created = _orders_created_inr(merchant_id=merchant_id)
    refunded = _refunded_inr(merchant_id=merchant_id)

    return {
        "merchant_id": merchant_id,  # None means "global, across every merchant"
        "total_revenue_inr": captured,
        "captured_inr": captured,
        "orders_created_inr": orders_created,
        "refunded_inr": refunded,
        "net_revenue_inr": captured - refunded,
        "revenue_by_actor": {a: _captured_inr(a, merchant_id) for a in ACTORS},
        "orders_created_by_actor": {a: _orders_created_inr(a, merchant_id) for a in ACTORS},
        "refunded_by_actor": {a: _refunded_inr(a, merchant_id) for a in ACTORS},
        "checkout_conversion_rate": {
            "overall": _conversion_rate(merchant_id=merchant_id),
            "by_actor": {a: _conversion_rate(a, merchant_id) for a in ACTORS},
        },
        "upsell_shown_count": upsell_shown_count,
        "upsell_accepted_count": upsell_accepted_count,
        "upsell_acceptance_rate": upsell_acceptance_rate,
        "upsell_blocked_by_cap_count": _upsell_blocked_by_cap_count(merchant_id),
    }
