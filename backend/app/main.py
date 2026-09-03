"""
Merchant backend -- single source of truth.

Both the WhatsApp (human buyer) flow and the MCP server (AI buyer)
flow call these same endpoints. This is deliberate: it proves the
merchant is "transactable by an AI buyer end to end" using the exact
same checkout logic a human uses, not a separate toy path.
"""

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from . import catalog, cart, payments, audit, metrics, webhooks
from .guardrails import check_checkout_allowed, check_cart_reviewed, GuardrailBlocked

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
    customer_contact: str = "9876543210"
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

    cart.check_and_record_upsell_acceptance(req.session_id, req.product_id)
    audit.log_action(req.actor, req.session_id, "add_to_cart", "ok",
                      details={"product_id": req.product_id, "qty": req.qty})
    cart_product_ids = {li["product_id"] for li in updated_cart}
    upsell = catalog.get_upsell(req.product_id, cart_items=updated_cart, exclude_ids=cart_product_ids)
    if upsell:
        cart.record_upsell_suggested(req.session_id, upsell["product_id"])
        audit.log_action(req.actor, req.session_id, "upsell_shown", "ok",
                          details={"suggested_product_id": upsell["product_id"]})
    return {"cart": updated_cart, "total_inr": cart.cart_total(req.session_id), "upsell": upsell}


@app.get("/cart/{session_id}")
def view_cart(session_id: str):
    cart.mark_cart_reviewed(session_id)
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

    try:
        # ---- Guardrail check (bounded + gated) ----
        try:
            # cart_not_reviewed checked first -- a workflow precondition,
            # ahead of the stock/spending-cap checks below.
            check_cart_reviewed(cart.was_cart_reviewed(req.session_id))
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
                          amount_inr=total, details={"payment_link": result.get("short_url"),
                                                      "payment_link_id": result.get("id"), "note": note})

        cart.clear_cart(req.session_id)
        return {"payment_link": result.get("short_url"), "amount_inr": total, "note": note}
    finally:
        # Whatever happened -- blocked, failed, or succeeded -- the next
        # checkout attempt needs a fresh view_cart() call; the gate
        # can't be reused across multiple attempts.
        cart.clear_cart_reviewed(req.session_id)


@app.post("/webhook/razorpay")
async def razorpay_webhook(request: Request):
    """
    Confirms a payment was actually *completed*, not just that a
    payment link was created (payments.py / checkout()). Signature
    verification happens before anything in the body is trusted.
    """
    body = await request.body()
    signature = request.headers.get("X-Razorpay-Signature", "")
    result = webhooks.handle_webhook(body, signature)
    if not result["ok"]:
        raise HTTPException(400, result["reason"])
    return {"status": "ok"}


@app.get("/audit-trail")
def audit_trail(limit: int = 50):
    return {"entries": audit.get_trail(limit)}


@app.get("/metrics")
def get_metrics():
    return metrics.get_metrics()


@app.get("/health")
def health():
    return {"status": "ok"}
