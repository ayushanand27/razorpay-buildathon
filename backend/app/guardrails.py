"""
Guardrails layer.

This is what makes agent-initiated money actions "bounded and gated"
instead of an agent just being handed a payment API and trusted blindly.
Modeled loosely on the consent + per-merchant spending-limit pattern
NPCI's UAP and Google's AP2 both use: a spending cap set in advance,
checked on every attempt, independent of which actor (human or AI)
is driving the checkout.
"""

import sqlite3

from . import audit

# Per-transaction spending cap for AI-agent-initiated purchases.
# A human on WhatsApp is not capped the same way because a human is
# already the accountable party; an AI buyer acting autonomously is
# capped to keep the "bounded" property real, not just claimed.
AI_AGENT_SPENDING_CAP_INR = 2000

# Cumulative cap across ALL of an AI agent's transactions today --
# the per-transaction cap alone doesn't stop the same agent running
# many separate under-the-cap purchases back to back. 2.5x the
# per-transaction cap: enough for a handful of real purchases in a
# day, not an unbounded number.
AI_AGENT_DAILY_SPENDING_CAP_INR = 5000


class GuardrailBlocked(Exception):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def check_cart_reviewed(reviewed: bool):
    """Raises GuardrailBlocked if the buyer hasn't reviewed the cart
    (via view_cart / GET /cart/{session_id}) since the last checkout
    attempt for this session -- the server-enforced half of "gated",
    not just an MCP tool docstring telling the agent to behave."""
    if not reviewed:
        raise GuardrailBlocked("cart_not_reviewed")


def _ai_agent_spend_today() -> float:
    """Sum of this AI agent's completed transactions (checkout_payment,
    ok or retried -- same "paid" definition metrics.py already uses)
    logged today, across every session -- computed directly over the
    existing audit trail, no separate running-total store."""
    conn = audit._get_conn()
    try:
        row = conn.execute(
            """
            SELECT COALESCE(SUM(amount_inr), 0.0) FROM audit_log
            WHERE actor = 'ai_agent_mcp'
              AND action = 'checkout_payment'
              AND status IN ('ok', 'retried')
              AND date(timestamp, 'unixepoch', 'localtime') = date('now', 'localtime')
            """
        ).fetchone()
        return row[0]
    finally:
        conn.close()


def check_checkout_allowed(actor: str, amount_inr: float, stock: int):
    """Raises GuardrailBlocked if this checkout should not proceed."""
    if stock <= 0:
        raise GuardrailBlocked("out_of_stock")

    if actor == "ai_agent_mcp":
        if amount_inr > AI_AGENT_SPENDING_CAP_INR:
            raise GuardrailBlocked(
                f"amount_inr {amount_inr} exceeds AI agent spending cap of {AI_AGENT_SPENDING_CAP_INR}"
            )

        if _ai_agent_spend_today() + amount_inr > AI_AGENT_DAILY_SPENDING_CAP_INR:
            raise GuardrailBlocked("daily_spending_cap_exceeded")

    return True
