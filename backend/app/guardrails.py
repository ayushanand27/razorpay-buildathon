"""
Guardrails layer.

This is what makes agent-initiated money actions "bounded and gated"
instead of an agent just being handed a payment API and trusted blindly.
Modeled loosely on the consent + per-merchant spending-limit pattern
NPCI's UAP and Google's AP2 both use: a spending cap set in advance,
checked on every attempt, independent of which actor (human or AI)
is driving the checkout.
"""

# Per-transaction spending cap for AI-agent-initiated purchases.
# A human on WhatsApp is not capped the same way because a human is
# already the accountable party; an AI buyer acting autonomously is
# capped to keep the "bounded" property real, not just claimed.
AI_AGENT_SPENDING_CAP_INR = 2000


class GuardrailBlocked(Exception):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def check_checkout_allowed(actor: str, amount_inr: float, stock: int):
    """Raises GuardrailBlocked if this checkout should not proceed."""
    if stock <= 0:
        raise GuardrailBlocked("out_of_stock")

    if actor == "ai_agent_mcp" and amount_inr > AI_AGENT_SPENDING_CAP_INR:
        raise GuardrailBlocked(
            f"amount_inr {amount_inr} exceeds AI agent spending cap of {AI_AGENT_SPENDING_CAP_INR}"
        )

    return True
