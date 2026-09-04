"""
Merchant backend -- single source of truth.

Split payment rails:
  - Human (POST /checkout): Razorpay Payment Links -- a human opens
    the returned URL to pay. Guarded by guardrails.py (cart review +
    stock only -- a human carries no spending warrant).
  - Agent (POST /agent/pay): Razorpay Orders API -- an AI agent has no
    browser to open a link in. Guarded by policy.py's 8-rule
    deterministic decision engine (warrant validity, merchant match,
    categories, price-tamper check, per-tx/daily caps, cart review,
    stock), then self-completed via the same signed-webhook capture
    path a real Razorpay webhook would use (see webhooks.py).

Who's calling (the "actor") is never trusted from the request body --
it's looked up server-side from the session the caller authenticated
into first (see sessions.py: POST /session/human, POST /session/agent).
"""

import json
import uuid

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from . import catalog, cart, payments, audit, metrics, webhooks, sessions, orders, policy, nlu, guardrails
from .guardrails import check_checkout_allowed, check_cart_reviewed, GuardrailBlocked

app = FastAPI(title="Agentic Commerce Demo Backend")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class AgentWarrantRequest(BaseModel):
    warrant: dict
    signature: str


class AddToCartRequest(BaseModel):
    session_id: str
    product_id: str
    qty: int = 1


class CheckoutRequest(BaseModel):
    session_id: str
    idempotency_key: str
    customer_contact: str = "9876543210"
    simulate_failure: bool = False


class AgentPayRequest(BaseModel):
    session_id: str
    idempotency_key: str
    confirm: bool


class NluTurnRequest(BaseModel):
    session_id: str
    text: str


def _require_session(session_id: str) -> dict:
    session = sessions.get_session(session_id)
    if not session:
        raise HTTPException(401, "invalid_session")
    return session


def _max_cart_total_for_upsell(session: dict) -> float:
    """The ceiling an upsell suggestion must not push the cart total
    past -- an agent's remaining warrant cap (whichever of per-tx or
    daily-remaining is tighter), or the merchant's flat MAX_ORDER_INR
    for a human. Used ONLY to decide whether to suggest an add-on, not
    to authorize anything -- the real cap re-check happens again, from
    scratch, at checkout/pay time."""
    if session["actor"] == sessions.AGENT_ACTOR:
        warrant = session["warrant"]
        spend_today = audit.captured_spend_today(sessions.AGENT_ACTOR)
        daily_remaining = max(warrant["daily_cap_inr"] - spend_today, 0.0)
        return min(warrant["per_tx_cap_inr"], daily_remaining)
    return guardrails.MAX_ORDER_INR


@app.post("/session/human")
def session_human():
    session_id = sessions.create_human_session()
    return {"session_id": session_id, "actor": sessions.HUMAN_ACTOR}


@app.post("/session/agent")
def session_agent(req: AgentWarrantRequest):
    try:
        session_id = sessions.create_agent_session(req.warrant, req.signature)
    except sessions.WarrantInvalid as e:
        raise HTTPException(401, f"invalid_warrant: {e.reason}")
    return {"session_id": session_id, "actor": sessions.AGENT_ACTOR}


@app.get("/catalog")
def get_catalog(category: str | None = None):
    return {"products": catalog.list_products(category)}


@app.post("/cart/add")
def add_to_cart(req: AddToCartRequest):
    session = _require_session(req.session_id)
    actor = session["actor"]

    product = catalog.get_product(req.product_id)
    if not product:
        audit.log_action(actor, req.session_id, "add_to_cart", "failed",
                          details={"reason": "product_not_found", "product_id": req.product_id})
        raise HTTPException(404, "product_not_found")

    updated_cart, err = cart.add_to_cart(req.session_id, req.product_id, req.qty)
    if err:
        audit.log_action(actor, req.session_id, "add_to_cart", "failed", details={"reason": err})
        raise HTTPException(400, err)

    cart.check_and_record_upsell_acceptance(req.session_id, req.product_id)
    audit.log_action(actor, req.session_id, "add_to_cart", "ok",
                      details={"product_id": req.product_id, "qty": req.qty})
    cart_product_ids = {li["product_id"] for li in updated_cart}
    max_cart_total_inr = _max_cart_total_for_upsell(session)
    upsell, blocked = catalog.get_upsell(req.product_id, cart_items=updated_cart,
                                          exclude_ids=cart_product_ids,
                                          max_cart_total_inr=max_cart_total_inr)
    if upsell:
        cart.record_upsell_suggested(req.session_id, upsell["product_id"])
        audit.log_action(actor, req.session_id, "upsell_shown", "ok",
                          details={"suggested_product_id": upsell["product_id"]})
    elif blocked:
        audit.log_action(actor, req.session_id, "upsell_blocked", "blocked",
                          details={"candidate_product_id": blocked["product_id"], "reason": blocked["reason"]})
    return {"cart": updated_cart, "total_inr": cart.cart_total(req.session_id), "upsell": upsell}


@app.get("/cart/{session_id}")
def view_cart(session_id: str):
    _require_session(session_id)
    cart.mark_cart_reviewed(session_id)
    return {"cart": cart.get_cart(session_id), "total_inr": cart.cart_total(session_id)}


def _format_catalog_reply(products: list[dict]) -> str:
    if not products:
        return "Nothing matches that in the catalog right now."
    lines = ["Here's what we have:"]
    for p in products:
        line = f"{p['id']} — {p['name']}"
        line += " (out of stock)" if p["availability"] == "out_of_stock" else f" — Rs.{p['price_inr']}"
        lines.append(line)
    lines.append("Reply like: add sku_001")
    return "\n".join(lines)


def _format_add_results_reply(results: list[dict]) -> str:
    lines = []
    for r in results:
        added = r["cart"][-1] if r["cart"] else None
        if added:
            lines.append(f"Added {added['name']}. Cart total: Rs.{r['total_inr']}")
        if r.get("upsell"):
            u = r["upsell"]
            lines.append(f"💡 {u['name']} — Rs.{u['price_inr']}\n{u['reason']}\nReply: add {u['product_id']}")
    return "\n".join(lines) if lines else "Couldn't add that."


def _format_cart_reply(data: dict) -> str:
    if not data["cart"]:
        return "Your cart is empty."
    lines = ["Your cart:"]
    for li in data["cart"]:
        lines.append(f"{li['name']} x{li['qty']} — Rs.{li['qty'] * li['price_inr']}")
    lines.append(f"\nTotal: Rs.{data['total_inr']}")
    return "\n".join(lines)


@app.post("/nlu/turn")
def nlu_turn(req: NluTurnRequest):
    """
    Free-text fallback for the human web chat, when the message doesn't
    match one of the hardcoded commands. Groq does INTENT CLASSIFICATION
    ONLY -- it picks a tool name (browse | add | view_cart | checkout |
    clarify) and, for `add`, which real product_ids are meant; see
    nlu.py. It never returns a price and never makes an allow/confirm
    decision. Every tool this endpoint executes is the exact same
    function the hardcoded chat commands (and /checkout directly) call
    -- guardrails, idempotency, and the cart-review gate all apply
    identically; NLU has no way to reach payments.py itself.
    """
    session = _require_session(req.session_id)
    actor = session["actor"]

    plan = nlu.parse_turn(req.text)
    tool = plan["tool"]

    try:
        if tool == "browse":
            data = get_catalog(category=plan.get("category"))
            products = data["products"]
            ceiling = plan.get("price_ceiling_inr")
            if ceiling is not None:
                products = [p for p in products if p["price_inr"] <= ceiling]
            return {"reply": _format_catalog_reply(products), "tool": "browse", "data": {"products": products}}

        if tool == "add":
            results = []
            for item in plan["items"]:
                add_req = AddToCartRequest(session_id=req.session_id, product_id=item["product_id"], qty=item["qty"])
                results.append(add_to_cart(add_req))
            return {"reply": _format_add_results_reply(results), "tool": "add", "data": results}

        if tool == "view_cart":
            data = view_cart(req.session_id)
            return {"reply": _format_cart_reply(data), "tool": "view_cart", "data": data}

        if tool == "checkout":
            if actor != sessions.HUMAN_ACTOR:
                return {"reply": "Checkout via chat is only available for the human buyer flow.", "tool": "clarify"}
            checkout_req = CheckoutRequest(session_id=req.session_id, idempotency_key=str(uuid.uuid4()))
            data = checkout(checkout_req)
            return {"reply": f"All set! Pay here: {data['payment_link']}\nAmount: Rs.{data['amount_inr']}",
                    "tool": "checkout", "data": data}

        return {"reply": plan.get("message", nlu.FALLBACK_MESSAGE), "tool": "clarify"}

    except HTTPException as e:
        return {"reply": f"Couldn't do that: {e.detail}", "tool": "clarify", "error": e.detail}


@app.post("/checkout")
def checkout(req: CheckoutRequest):
    """
    HUMAN rail only -- Payment Links. Every attempt -- allowed,
    blocked, failed, or recovered -- is written to the audit trail
    before the function returns. Idempotent on
    (session_id, idempotency_key).
    """
    session = _require_session(req.session_id)
    actor = session["actor"]
    if actor != sessions.HUMAN_ACTOR:
        raise HTTPException(400, "agents must use /agent/pay -- /checkout is the human (Payment Links) rail")

    existing = orders.find_existing_order(req.session_id, req.idempotency_key)
    if existing:
        audit.log_action(actor, req.session_id, "checkout_replayed", "ok",
                          amount_inr=existing["total_inr"],
                          details={"idempotency_key": req.idempotency_key, "order_id": existing["order_id"]})
        return existing["response"]

    line_items = cart.get_cart(req.session_id)
    if not line_items:
        raise HTTPException(400, "cart_empty")

    total = cart.cart_total(req.session_id)

    try:
        # ---- Guardrail check (bounded + gated) ----
        try:
            # cart_not_reviewed checked first -- a workflow precondition,
            # ahead of the stock check below.
            check_cart_reviewed(cart.was_cart_reviewed(req.session_id))
            check_checkout_allowed(line_items)
        except GuardrailBlocked as e:
            audit.log_action(actor, req.session_id, "checkout_attempt", "blocked",
                              amount_inr=total, details={"reason": e.reason})
            raise HTTPException(403, f"blocked_by_guardrail: {e.reason}")

        audit.log_action(actor, req.session_id, "checkout_attempt", "ok", amount_inr=total)

        # ---- Payment (with graceful-failure demo path) ----
        description = f"Order for {req.session_id} ({len(line_items)} item(s))"
        result, note, first_body, retry_body = payments.create_order_with_retry(
            total, description, req.customer_contact, simulate_failure=req.simulate_failure
        )

        if result is None:
            audit.log_action(actor, req.session_id, "checkout_payment", "failed",
                              amount_inr=total, details={"reason": note,
                                                          "first_attempt_response": first_body,
                                                          "retry_attempt_response": retry_body})
            raise HTTPException(502, f"payment_failed: {note}")

        status = "retried" if note else "ok"
        payment_link_id = result.get("id")
        order_id = orders.new_order_id()
        response = {"payment_link": result.get("short_url"), "amount_inr": total,
                    "note": note, "order_id": order_id}
        orders.create_order(order_id, req.session_id, actor, line_items, total,
                             req.idempotency_key, response, payment_link_id=payment_link_id)

        audit.log_action(actor, req.session_id, "checkout_payment", status,
                          amount_inr=total, details={"payment_link": result.get("short_url"),
                                                      "payment_link_id": payment_link_id,
                                                      "order_id": order_id, "note": note,
                                                      "first_attempt_response": first_body,
                                                      "retry_attempt_response": retry_body})

        cart.clear_cart(req.session_id)
        return response
    finally:
        # Whatever happened -- blocked, failed, or succeeded -- the next
        # checkout attempt needs a fresh view_cart() call; the gate
        # can't be reused across multiple attempts.
        cart.clear_cart_reviewed(req.session_id)


@app.post("/agent/pay")
def agent_pay(req: AgentPayRequest):
    """
    AGENT rail only -- Razorpay Orders API, immediately self-completed
    via the same signed-webhook capture path a real Razorpay webhook
    would use (see webhooks.py). Every policy decision (allow OR
    block) is logged in full -- see policy.Decision -- before this
    function does anything else. Idempotent on
    (session_id, idempotency_key).
    """
    session = _require_session(req.session_id)
    actor = session["actor"]
    if actor != sessions.AGENT_ACTOR:
        raise HTTPException(400, "only agent sessions may use /agent/pay -- humans use /checkout")

    if not req.confirm:
        raise HTTPException(400, "pay requires confirm=True after showing the buyer the cart total")

    existing = orders.find_existing_order(req.session_id, req.idempotency_key)
    if existing:
        audit.log_action(actor, req.session_id, "checkout_replayed", "ok",
                          amount_inr=existing["total_inr"],
                          details={"idempotency_key": req.idempotency_key, "order_id": existing["order_id"]})
        return existing["response"]

    line_items = cart.get_cart(req.session_id)
    if not line_items:
        raise HTTPException(400, "cart_empty")

    try:
        warrant = session["warrant"]
        warrant_signature = session["warrant_signature"]
        spend_today = audit.captured_spend_today(sessions.AGENT_ACTOR)
        cart_reviewed = cart.was_cart_reviewed(req.session_id)

        proposal = policy.build_proposal(warrant, warrant_signature, line_items, spend_today, cart_reviewed)
        decision = policy.evaluate(proposal)

        # "Log the full decision JSON" -- every attempt, allow or
        # block, before anything else happens.
        audit.log_action(actor, req.session_id, "policy_decision",
                          "ok" if decision.allow else "blocked",
                          amount_inr=proposal.proposed_total_inr, details=decision.to_dict())

        if not decision.allow:
            raise HTTPException(403, f"blocked_by_policy: {decision.reason}")

        total = proposal.proposed_total_inr

        # Mirrors the human rail's checkout_attempt/ok entry so both
        # rails' conversion-rate numbers in metrics.py stay comparable
        # -- policy_decision above is the new, detailed explainability
        # layer; this is the existing coarse-grained one.
        audit.log_action(actor, req.session_id, "checkout_attempt", "ok", amount_inr=total)

        receipt = f"agent-{req.session_id}-{req.idempotency_key[:8]}"
        try:
            result = payments.create_agent_order(total, receipt, notes={"session_id": req.session_id})
        except payments.PaymentFailure as e:
            audit.log_action(actor, req.session_id, "checkout_payment", "failed",
                              amount_inr=total, details={"reason": e.reason, "response": e.response_body})
            raise HTTPException(502, f"order_creation_failed: {e.reason}")

        razorpay_order_id = result.get("id")
        order_id = orders.new_order_id()
        response = {"order_id": order_id, "razorpay_order_id": razorpay_order_id,
                    "amount_inr": total, "status": "pending_capture"}
        orders.create_order(order_id, req.session_id, actor, line_items, total,
                             req.idempotency_key, response, razorpay_order_id=razorpay_order_id)

        audit.log_action(actor, req.session_id, "checkout_payment", "ok", amount_inr=total,
                          details={"razorpay_order_id": razorpay_order_id, "order_id": order_id})

        cart.clear_cart(req.session_id)

        # Self-complete: an AI agent has no browser to actually pay a
        # real order in, so this demo confirms it immediately, through
        # the EXACT SAME verification+capture function a real Razorpay
        # webhook call would go through (see webhooks.handle_webhook).
        capture_body = webhooks.build_capture_payload(razorpay_order_id=razorpay_order_id, amount_inr=total)
        body_bytes = json.dumps(capture_body).encode("utf-8")
        signature = webhooks.sign_body(body_bytes)
        capture_result = webhooks.handle_webhook(body_bytes, signature, source="agent_pay_self_capture")

        response["status"] = "captured" if capture_result["ok"] else "capture_failed"
        return response
    finally:
        cart.clear_cart_reviewed(req.session_id)


@app.get("/agent/remaining-cap")
def agent_remaining_cap(session_id: str):
    session = _require_session(session_id)
    if session["actor"] != sessions.AGENT_ACTOR:
        raise HTTPException(400, "remaining-cap only applies to agent sessions")
    warrant = session["warrant"]
    spend_today = audit.captured_spend_today(sessions.AGENT_ACTOR)
    return {
        "per_tx_cap_inr": warrant["per_tx_cap_inr"],
        "daily_remaining_inr": max(warrant["daily_cap_inr"] - spend_today, 0.0),
        "warrant_expires_at": warrant["expires_at"],
    }


@app.get("/agent/explain-last-block")
def agent_explain_last_block(session_id: str):
    _require_session(session_id)
    for entry in audit.get_trail(limit=200):
        if entry["actor_id"] == session_id and entry["action"] == "policy_decision" and entry["status"] == "blocked":
            return {"decision": json.loads(entry["details"]), "timestamp": entry["timestamp"]}
    return {"decision": None, "message": "no prior blocked decision for this session"}


@app.post("/webhook/razorpay")
async def razorpay_webhook(request: Request):
    """
    Confirms a payment was actually *completed*, not just that a
    payment link or order was created. Signature verification happens
    before anything in the body is trusted.
    """
    body = await request.body()
    signature = request.headers.get("X-Razorpay-Signature", "")
    result = webhooks.handle_webhook(body, signature, source="webhook")
    if not result["ok"]:
        raise HTTPException(400, result["reason"])
    return {"status": "ok"}


@app.post("/demo/simulate-capture")
async def demo_simulate_capture(request: Request):
    """
    Demo/test stand-in for Razorpay actually calling POST
    /webhook/razorpay in production -- there's no public URL for
    Razorpay to call in this local demo. Runs through the EXACT SAME
    webhooks.handle_webhook() signature-verification and capture logic
    as the real endpoint above (same RAZORPAY_WEBHOOK_SECRET, same
    HMAC-over-raw-body scheme) -- only the caller differs. POST
    /agent/pay calls the same underlying function directly (in-process)
    to self-complete a purchase; this HTTP endpoint exists for manual
    testing and DEMO_SCRIPT.md walkthroughs of the human rail's capture
    step too.
    """
    body = await request.body()
    signature = request.headers.get("X-Razorpay-Signature", "")
    result = webhooks.handle_webhook(body, signature, source="demo_simulate_capture")
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
