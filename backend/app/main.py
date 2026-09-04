"""
Merchant backend -- single source of truth. Multi-tenant: every
session belongs to exactly one merchant (merchants.py), and every
catalog/cart/order/policy operation is scoped to that merchant. Two
merchants can reuse the same SKU id without collision, and an agent
warrant signed for one merchant can never mint a session against, or
spend against the cap of, a different one -- see sessions.py and
policy.py.

New merchant-scoped endpoints:
  GET  /merchants
  GET  /merchants/{merchant_id}/catalog
  POST /merchants/{merchant_id}/session/human
  POST /merchants/{merchant_id}/session/agent
The original un-prefixed /catalog, /session/human, /session/agent
routes still work, as thin aliases for merchant_id="demo_merchant" --
kept so the existing web chat and MCP server need no changes.

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
import threading
import uuid
from collections import defaultdict

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from . import catalog, cart, payments, audit, metrics, webhooks, sessions, orders, policy, nlu, guardrails, merchants
from .guardrails import check_checkout_allowed, check_cart_reviewed, GuardrailBlocked

app = FastAPI(title="Agentic Commerce Demo Backend")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Idempotency (both rails) is a check-then-act sequence spanning
# several steps (guardrail checks, a payment API call, order creation)
# -- two concurrent requests carrying the SAME (session_id,
# idempotency_key), e.g. a genuine network retry racing the original,
# could otherwise both pass the "does this order already exist?" check
# before either has created it, producing two orders instead of one.
# A per-session lock serializes only that one buyer's own concurrent
# attempts; different sessions still process fully in parallel.
_session_locks: dict[str, threading.Lock] = defaultdict(threading.Lock)
_session_locks_guard = threading.Lock()


def _get_session_lock(session_id: str) -> threading.Lock:
    with _session_locks_guard:
        return _session_locks[session_id]


class AgentWarrantRequest(BaseModel):
    warrant: dict
    signature: str


class AddToCartRequest(BaseModel):
    session_id: str
    product_id: str
    qty: int = 1


class RemoveFromCartRequest(BaseModel):
    session_id: str
    product_id: str


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


class RefundRequest(BaseModel):
    session_id: str
    order_id: str


def _require_session(session_id: str) -> dict:
    session = sessions.get_session(session_id)
    if not session:
        raise HTTPException(401, "invalid_session")
    return session


def _require_merchant(merchant_id: str) -> dict:
    merchant = merchants.get_merchant(merchant_id)
    if not merchant:
        raise HTTPException(404, "unknown_merchant")
    return merchant


def _agent_spend_today(merchant_id: str) -> float:
    """CAPTURED spend today AT THIS MERCHANT, PLUS spend still sitting
    in orders that are 'created' but not yet captured or failed today
    (see orders.pending_spend_today) -- closes the gap where the daily
    cap only counted captured money and an order stuck mid-flight
    (e.g. the process crashed between order-creation and self-capture)
    could silently dodge it. A warrant's daily cap is itself
    per-(agent, merchant), so both terms are scoped to merchant_id --
    spend at a different merchant never affects this one's cap."""
    return (audit.captured_spend_today(merchant_id, sessions.AGENT_ACTOR)
            + orders.pending_spend_today(merchant_id, sessions.AGENT_ACTOR))


def _max_cart_total_for_upsell(session: dict) -> float:
    """The ceiling an upsell suggestion must not push the cart total
    past -- an agent's remaining warrant cap (whichever of per-tx or
    daily-remaining is tighter), or this merchant's own max_order_inr
    for a human. Used ONLY to decide whether to suggest an add-on, not
    to authorize anything -- the real cap re-check happens again, from
    scratch, at checkout/pay time."""
    merchant_id = session["merchant_id"]
    if session["actor"] == sessions.AGENT_ACTOR:
        warrant = session["warrant"]
        daily_remaining = max(warrant["daily_cap_inr"] - _agent_spend_today(merchant_id), 0.0)
        return min(warrant["per_tx_cap_inr"], daily_remaining)
    return merchants.get_max_order_inr(merchant_id)


def _list_catalog(merchant_id: str, category: str | None = None) -> list[dict]:
    return catalog.list_products(merchant_id, category)


@app.get("/merchants")
def list_merchants():
    return {"merchants": merchants.list_merchants()}


@app.get("/merchants/{merchant_id}/catalog")
def get_merchant_catalog(merchant_id: str, category: str | None = None):
    _require_merchant(merchant_id)
    return {"products": _list_catalog(merchant_id, category)}


@app.post("/merchants/{merchant_id}/session/human")
def merchant_session_human(merchant_id: str):
    _require_merchant(merchant_id)
    session_id = sessions.create_human_session(merchant_id)
    return {"session_id": session_id, "actor": sessions.HUMAN_ACTOR, "merchant_id": merchant_id}


@app.post("/merchants/{merchant_id}/session/agent")
def merchant_session_agent(merchant_id: str, req: AgentWarrantRequest):
    _require_merchant(merchant_id)
    try:
        session_id = sessions.create_agent_session(merchant_id, req.warrant, req.signature)
    except sessions.WarrantInvalid as e:
        raise HTTPException(401, f"invalid_warrant: {e.reason}")
    return {"session_id": session_id, "actor": sessions.AGENT_ACTOR, "merchant_id": merchant_id}


# ---- Backward-compatible aliases -- default to demo_merchant, so the
# existing web chat and MCP server (neither of which know merchants
# exist) keep working unchanged. ----

@app.post("/session/human")
def session_human():
    return merchant_session_human("demo_merchant")


@app.post("/session/agent")
def session_agent(req: AgentWarrantRequest):
    return merchant_session_agent("demo_merchant", req)


@app.get("/catalog")
def get_catalog(category: str | None = None):
    return {"products": _list_catalog("demo_merchant", category)}


@app.post("/cart/add")
def add_to_cart(req: AddToCartRequest):
    session = _require_session(req.session_id)
    actor = session["actor"]
    merchant_id = session["merchant_id"]

    product = catalog.get_product(merchant_id, req.product_id)
    if not product:
        audit.log_action(actor, req.session_id, "add_to_cart", "failed",
                          details={"reason": "product_not_found", "product_id": req.product_id})
        raise HTTPException(404, "product_not_found")

    updated_cart, err = cart.add_to_cart(merchant_id, req.session_id, req.product_id, req.qty)
    if err:
        audit.log_action(actor, req.session_id, "add_to_cart", "failed", details={"reason": err})
        raise HTTPException(400, err)

    cart.check_and_record_upsell_acceptance(req.session_id, req.product_id)
    audit.log_action(actor, req.session_id, "add_to_cart", "ok",
                      details={"product_id": req.product_id, "qty": req.qty})
    cart_product_ids = {li["product_id"] for li in updated_cart}
    max_cart_total_inr = _max_cart_total_for_upsell(session)
    upsell, blocked = catalog.get_upsell(merchant_id, req.product_id, cart_items=updated_cart,
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


@app.post("/cart/remove")
def remove_from_cart(req: RemoveFromCartRequest):
    session = _require_session(req.session_id)
    actor = session["actor"]

    updated_cart, err = cart.remove_from_cart(req.session_id, req.product_id)
    if err:
        audit.log_action(actor, req.session_id, "remove_from_cart", "failed",
                          details={"reason": err, "product_id": req.product_id})
        raise HTTPException(404, err)

    audit.log_action(actor, req.session_id, "remove_from_cart", "ok",
                      details={"product_id": req.product_id})
    return {"cart": updated_cart, "total_inr": cart.cart_total(req.session_id)}


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


def _format_remove_results_reply(results: list[dict]) -> str:
    if not results:
        return "Couldn't find that in your cart."
    last = results[-1]
    return f"Removed. Cart total: Rs.{last['total_inr']}"


@app.post("/nlu/turn")
def nlu_turn(req: NluTurnRequest):
    """
    Free-text fallback for the human web chat, when the message doesn't
    match one of the hardcoded commands. Groq does INTENT CLASSIFICATION
    ONLY -- it picks a tool name (browse | add | remove | view_cart |
    checkout | clarify) and, for `add`/`remove`, which real product_ids
    are meant (scoped to this session's merchant); see nlu.py. It never
    returns a price and never makes an allow/confirm decision. Every
    tool this endpoint executes is the exact same function the
    hardcoded chat commands (and /checkout directly) call -- guardrails,
    idempotency, and the cart-review gate all apply identically; NLU
    has no way to reach payments.py itself.
    """
    session = _require_session(req.session_id)
    actor = session["actor"]
    merchant_id = session["merchant_id"]

    plan = nlu.parse_turn(merchant_id, req.text)
    tool = plan["tool"]

    try:
        if tool == "browse":
            products = _list_catalog(merchant_id, plan.get("category"))
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

        if tool == "remove":
            results = []
            for item in plan["items"]:
                remove_req = RemoveFromCartRequest(session_id=req.session_id, product_id=item["product_id"])
                try:
                    results.append(remove_from_cart(remove_req))
                except HTTPException:
                    pass  # that item just wasn't in the cart -- skip it, not a hard failure
            return {"reply": _format_remove_results_reply(results), "tool": "remove", "data": results}

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
    merchant_id = session["merchant_id"]
    if actor != sessions.HUMAN_ACTOR:
        raise HTTPException(400, "agents must use /agent/pay -- /checkout is the human (Payment Links) rail")

    # Serializes this SESSION's own concurrent checkout attempts (e.g. a
    # genuine network retry racing the original request) so the
    # idempotency check-then-create sequence below can't have two
    # callers both pass the "does this order exist yet?" check before
    # either creates it. Different sessions are unaffected.
    with _get_session_lock(req.session_id):
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
                check_checkout_allowed(merchant_id, line_items)
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
            orders.create_order(order_id, merchant_id, req.session_id, actor, line_items, total,
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
    merchant_id = session["merchant_id"]
    if actor != sessions.AGENT_ACTOR:
        raise HTTPException(400, "only agent sessions may use /agent/pay -- humans use /checkout")

    if not req.confirm:
        raise HTTPException(400, "pay requires confirm=True after showing the buyer the cart total")

    # See /checkout's identical comment -- same idempotency race, same fix.
    with _get_session_lock(req.session_id):
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
            spend_today = _agent_spend_today(merchant_id)
            cart_reviewed = cart.was_cart_reviewed(req.session_id)

            proposal = policy.build_proposal(merchant_id, warrant, warrant_signature, line_items,
                                              spend_today, cart_reviewed)
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
            orders.create_order(order_id, merchant_id, req.session_id, actor, line_items, total,
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
            webhooks.handle_webhook(body_bytes, signature, source="agent_pay_self_capture")

            # handle_webhook()'s own "ok" reflects whether the WEBHOOK was
            # verified/processed, not whether the capture inside it
            # actually succeeded (a capture_failed order still acks the
            # webhook, same as a real Razorpay deployment would) -- re-read
            # the order's real status rather than trusting that flag here.
            final_order = orders.get_order(order_id)
            response["status"] = final_order["status"]
            orders.update_response(order_id, response)
            return response
        finally:
            cart.clear_cart_reviewed(req.session_id)


@app.get("/agent/remaining-cap")
def agent_remaining_cap(session_id: str):
    session = _require_session(session_id)
    if session["actor"] != sessions.AGENT_ACTOR:
        raise HTTPException(400, "remaining-cap only applies to agent sessions")
    warrant = session["warrant"]
    spend_today = _agent_spend_today(session["merchant_id"])
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


@app.post("/refund")
def refund(req: RefundRequest):
    """
    Reverses a CAPTURED order on either rail -- real Razorpay Refunds
    API call first (payments.create_refund), then, only if that
    succeeds, restores stock and marks the order refunded
    (orders.refund_order). A session may only refund its OWN orders.
    Full refunds only; refunding an already-refunded order is
    idempotent (no-op, still returns success).
    """
    session = _require_session(req.session_id)
    actor = session["actor"]

    order = orders.get_order(req.order_id)
    if not order:
        raise HTTPException(404, "order_not_found")
    if order["session_id"] != req.session_id:
        raise HTTPException(403, "order_belongs_to_a_different_session")
    if order["status"] == "refunded":
        return {"order_id": req.order_id, "status": "refunded", "amount_inr": order["total_inr"]}
    if order["status"] != "captured":
        raise HTTPException(400, f"order_not_captured: status={order['status']}")

    try:
        payments.create_refund(order["payment_id"], order["total_inr"])
    except payments.PaymentFailure as e:
        audit.log_action(actor, req.session_id, "refund", "failed",
                          amount_inr=order["total_inr"],
                          details={"reason": e.reason, "response": e.response_body, "order_id": req.order_id})
        raise HTTPException(502, f"refund_failed: {e.reason}")

    ok, reason = orders.refund_order(req.order_id)
    if not ok:
        audit.log_action(actor, req.session_id, "refund", "failed",
                          amount_inr=order["total_inr"], details={"reason": reason, "order_id": req.order_id})
        raise HTTPException(400, reason)

    audit.log_action(actor, req.session_id, "refund", "ok",
                      amount_inr=order["total_inr"], details={"order_id": req.order_id})
    return {"order_id": req.order_id, "status": "refunded", "amount_inr": order["total_inr"]}


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
