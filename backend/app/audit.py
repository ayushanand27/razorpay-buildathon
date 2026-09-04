"""
Append-only audit trail -- db.AuditLog, part of the single shared
app.db (not a separate audit_trail.db anymore).

Every action that touches money or cart state MUST be logged here,
regardless of whether it came from the WhatsApp human-agent flow
or the MCP AI-buyer flow. This is what makes the system's actions
explainable after the fact -- the buildathon's stated bar for
Track 1 is "every money action explainable, bounded and gated" and
"show the audit trail" -- this module IS that audit trail.

merchant_id is now a REAL column (not resolved via a per-row session
lookup at query time), and get_trail() below REQUIRES one -- there is
no global/unauthenticated "every merchant's audit log" read path left
anywhere in this system; GET /merchants/{merchant_id}/audit-trail
(main.py) is the only route to this data, and it derives merchant_id
from the caller's own server-validated session, never from a query
param a client could set to a different merchant's id.
"""

import datetime

from sqlmodel import select

from . import db


def log_action(actor: str, actor_id: str, action: str, status: str,
                amount_inr: float | None = None, details: dict | None = None,
                merchant_id: str | None = None, session=None) -> int:
    """merchant_id is omitted only for the handful of pre-match webhook-
    meta entries logged before any order/session is resolved (see
    webhooks.py) -- every buyer-initiated action always has one and
    should always pass it explicitly.

    Pass an open `session` (a `with db.get_session() as s:` block) to
    make this insert commit-or-rollback together with whatever else is
    happening in that same block -- e.g. orders.create_order(...,
    session=s) right before it, so "the order exists" and "its audit
    entry exists" are one atomic database write, never one without the
    other. Omit `session` to commit standalone."""
    entry = db.AuditLog(merchant_id=merchant_id, actor=actor, actor_id=actor_id, action=action,
                         amount_inr=amount_inr, status=status, details=db.json_dumps(details))
    if session is not None:
        session.add(entry)
        session.flush()
        return entry.id
    with db.get_session() as s:
        s.add(entry)
        s.flush()
        return entry.id


def get_trail(merchant_id: str, limit: int = 100) -> list[dict]:
    """Strictly scoped to ONE merchant -- merchant_id is required, not
    optional. There is no call path in this module that returns rows
    across merchants; a caller wanting a platform-wide view has none
    (see main.py's docstring on GET /merchants/{merchant_id}/audit-trail)."""
    with db.get_session() as s:
        rows = s.exec(
            select(db.AuditLog)
            .where(db.AuditLog.merchant_id == merchant_id)
            .order_by(db.AuditLog.id.desc())
            .limit(limit)
        ).all()
    return [
        {
            "id": r.id, "timestamp": r.timestamp, "actor": r.actor, "actor_id": r.actor_id,
            "action": r.action, "amount_inr": r.amount_inr, "status": r.status, "details": r.details,
        }
        for r in rows
    ]


def captured_spend_today(merchant_id: str, actor: str) -> float:
    """Sum of this actor's CAPTURED (payment_confirmed, status=paid)
    transactions AT THIS MERCHANT today -- not merely order/link-created
    ones, and not spend at a DIFFERENT merchant (a warrant's daily cap
    is itself per-(agent, merchant), and merchant_id is now a real
    column here, so this is a direct WHERE filter -- no more resolving
    each row's actor_id back to a session to find its merchant).

    This is the `spend_today` input policy.evaluate() needs for its
    daily-cap rule; computed here (a plain query) rather than inside
    policy.py so that module stays a pure function of its inputs, with
    no I/O of its own."""
    today = datetime.date.today()
    with db.get_session() as s:
        rows = s.exec(
            select(db.AuditLog.amount_inr, db.AuditLog.timestamp)
            .where(db.AuditLog.merchant_id == merchant_id, db.AuditLog.actor == actor,
                   db.AuditLog.action == "payment_confirmed", db.AuditLog.status == "paid")
        ).all()
    return sum(
        amount_inr or 0.0 for amount_inr, timestamp in rows
        if datetime.datetime.fromtimestamp(timestamp).date() == today
    )
