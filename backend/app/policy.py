"""
Deterministic policy engine -- the single ALLOW/BLOCK authority for
POST /agent/pay (the agent-checkout rail). A pure function of its
inputs: the same proposal always yields the same decision. No LLM
anywhere in this module, or anywhere upstream of it -- money decisions
are 100% rule-based, on purpose.

evaluate() checks 8 rules, all of which must pass:
  1. warrant signature valid and not expired
  2. merchant_id matches
  3. every SKU in allowed_categories
  4. server price * qty == proposed total (tamper check)
  5. per-transaction cap
  6. daily cap
  7. cart_reviewed since last mutation
  8. no OOS (per line item, not min-across-cart)

Prices and stock always come from the SERVER catalog
(catalog.get_product()) -- a cart line item's own stored `price_inr`
(captured at add-to-cart time) is used ONLY as the "what the buyer
last saw" comparison target for rule 4, never as the amount actually
charged.
"""

import hmac
import time
from dataclasses import asdict, dataclass, field

from . import catalog, sessions as sessions_mod


@dataclass
class Decision:
    allow: bool
    reason: str
    remaining_cap_inr: float

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Proposal:
    merchant_id: str
    warrant: dict
    warrant_signature: str
    cart: list[dict]           # server-derived: product_id, qty, name, unit_price_inr, category
    proposed_total_inr: float  # what the cart's OWN stored line-item prices sum to (rule 4's comparison target)
    spend_today_inr: float
    cart_reviewed: bool
    now: float = field(default_factory=time.time)


def build_proposal(merchant_id: str, warrant: dict, warrant_signature: str, cart_line_items: list[dict],
                    spend_today_inr: float, cart_reviewed: bool, now: float | None = None) -> Proposal:
    """Builds a proposal from the cart's OWN stored line items (product_id,
    qty, price_inr captured at add-to-cart time) plus a fresh lookup of
    each product against the live server catalog, SCOPED TO merchant_id
    -- the two are kept separate on purpose so evaluate() can compare
    them (rule 4)."""
    server_cart = []
    proposed_total = 0.0
    for li in cart_line_items:
        product = catalog.get_product(merchant_id, li["product_id"])
        server_cart.append({
            "product_id": li["product_id"],
            "qty": li["qty"],
            "name": product["name"] if product else li.get("name"),
            "unit_price_inr": product["price_inr"] if product else None,
            "category": product["category"] if product else None,
            "stock": product["stock"] if product else 0,
        })
        proposed_total += li["qty"] * li.get("price_inr", 0)
    return Proposal(
        merchant_id=merchant_id, warrant=warrant, warrant_signature=warrant_signature, cart=server_cart,
        proposed_total_inr=proposed_total, spend_today_inr=spend_today_inr,
        cart_reviewed=cart_reviewed, now=now if now is not None else time.time(),
    )


def evaluate(proposal: Proposal) -> Decision:
    warrant = proposal.warrant

    # 1. warrant signature valid and not expired
    expected_signature = sessions_mod.sign_warrant(warrant)
    if not hmac.compare_digest(expected_signature, proposal.warrant_signature or ""):
        return Decision(False, "invalid_warrant_signature", 0.0)
    if warrant.get("expires_at", 0) < proposal.now:
        return Decision(False, "warrant_expired", 0.0)

    # 2. merchant_id matches -- the warrant's OWN claimed merchant_id
    # must match the merchant this session actually belongs to (not a
    # fixed global constant; sessions.create_agent_session() enforces
    # this at mint time too, this is the same check re-run at pay time
    # for defense in depth, same as the signature/expiry checks above).
    if warrant.get("merchant_id") != proposal.merchant_id:
        return Decision(False, "merchant_id_mismatch", 0.0)

    per_tx_cap = warrant["per_tx_cap_inr"]
    daily_cap = warrant["daily_cap_inr"]
    allowed_categories = warrant.get("allowed_categories") or []
    remaining_daily = max(daily_cap - proposal.spend_today_inr, 0.0)

    # 3. every SKU in allowed_categories
    for li in proposal.cart:
        if li["category"] not in allowed_categories:
            return Decision(False, f"category_not_allowed: {li['category']}", remaining_daily)

    # 4. server price * qty == proposed total (tamper check) -- ignores
    # whatever price the client's cart line items claim; recomputes
    # from the server catalog and requires the two to match. A cart
    # entry whose stored price no longer matches the live catalog
    # (tampered, corrupted, or simply stale) blocks here.
    server_total = sum(li["qty"] * (li["unit_price_inr"] or 0) for li in proposal.cart)
    if abs(server_total - proposal.proposed_total_inr) > 0.01:
        return Decision(False, "price_tampered", remaining_daily)

    # 5. per-transaction cap
    if server_total > per_tx_cap:
        return Decision(False, f"amount_inr {server_total} exceeds per-transaction cap of {per_tx_cap}", remaining_daily)

    # 6. daily cap
    if proposal.spend_today_inr + server_total > daily_cap:
        return Decision(False, "daily_spending_cap_exceeded", remaining_daily)

    # 7. cart_reviewed since last mutation
    if not proposal.cart_reviewed:
        return Decision(False, "cart_not_reviewed", remaining_daily)

    # 8. no OOS -- per line item, not min(stock) across the whole cart
    for li in proposal.cart:
        if li["qty"] > li["stock"]:
            return Decision(False, "out_of_stock", remaining_daily)

    return Decision(True, "ok", max(remaining_daily - server_total, 0.0))
