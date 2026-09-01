"""
Merchant backend -- single source of truth.

Both the WhatsApp (human buyer) flow and the MCP server (AI buyer)
flow call these same endpoints. This is deliberate: it proves the
merchant is "transactable by an AI buyer end to end" using the exact
same checkout logic a human uses, not a separate toy path.
"""

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from . import catalog, cart, payments, audit
from .guardrails import check_checkout_allowed, GuardrailBlocked

app = FastAPI(title="Agentic Commerce Demo Backend")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class AddToCartRequest(BaseModel):
    session_id: str
    actor: str  # "human_whatsapp" | "ai_agent_mcp"
    product_id: str
    qty: int = 1


class CheckoutRequest(BaseModel):
    session_id: str
    actor: str
    customer_contact: str = "9999999999"
    simulate_failure: bool = False


@app.get("/catalog")
def get_catalog(category: str | None = None):
    return {"products": catalog.list_products(category)}


@app.post("/cart/add")
def add_to_cart(req: AddToCartRequest):
    product = catalog.get_product(req.product_id)
    if not product:
        audit.log_action(req.actor, req.session_id, "add_to_cart", "failed",
                          details={"reason": "product_not_found", "product_id": req.product_id})
        raise HTTPException(404, "product_not_found")

    updated_cart, err = cart.add_to_cart(req.session_id, req.product_id, req.qty)
    if err:
        audit.log_action(req.actor, req.session_id, "add_to_cart", "failed", details={"reason": err})
        raise HTTPException(400, err)

    audit.log_action(req.actor, req.session_id, "add_to_cart", "ok",
                      details={"product_id": req.product_id, "qty": req.qty})
    return {"cart": updated_cart, "total_inr": cart.cart_total(req.session_id)}


@app.get("/cart/{session_id}")
def view_cart(session_id: str):
    return {"cart": cart.get_cart(session_id), "total_inr": cart.cart_total(session_id)}


@app.post("/checkout")
def checkout(req: CheckoutRequest):
    """
    The single gated money-action endpoint. Every attempt -- allowed,
    blocked, failed, or recovered -- is written to the audit trail
    before the function returns, so the trail is complete even when
    something goes wrong.
    """
    line_items = cart.get_cart(req.session_id)
    if not line_items:
        raise HTTPException(400, "cart_empty")

    total = cart.cart_total(req.session_id)

    # ---- Guardrail check (bounded + gated) ----
    try:
        # use the stock of the first out-of-stock item if any, else assume ok
        min_stock = min(
            (catalog.get_product(li["product_id"])["stock"] for li in line_items),
            default=1,
        )
        check_checkout_allowed(req.actor, total, min_stock)
    except GuardrailBlocked as e:
        audit.log_action(req.actor, req.session_id, "checkout_attempt", "blocked",
                          amount_inr=total, details={"reason": e.reason})
        raise HTTPException(403, f"blocked_by_guardrail: {e.reason}")

    audit.log_action(req.actor, req.session_id, "checkout_attempt", "ok", amount_inr=total)

    # ---- Payment (with graceful-failure demo path) ----
    description = f"Order for {req.session_id} ({len(line_items)} item(s))"
    result, note = payments.create_payment_link_with_retry(
        total, description, req.customer_contact, simulate_failure=req.simulate_failure
    )

    if result is None:
        audit.log_action(req.actor, req.session_id, "checkout_payment", "failed",
                          amount_inr=total, details={"reason": note})
        raise HTTPException(502, f"payment_failed: {note}")

    status = "retried" if note and "recovered" in note else "ok"
    audit.log_action(req.actor, req.session_id, "checkout_payment", status,
                      amount_inr=total, details={"payment_link": result.get("short_url"), "note": note})

    cart.clear_cart(req.session_id)
    return {"payment_link": result.get("short_url"), "amount_inr": total, "note": note}


@app.get("/audit-trail")
def audit_trail(limit: int = 50):
    return {"entries": audit.get_trail(limit)}


@app.get("/health")
def health():
    return {"status": "ok"}
