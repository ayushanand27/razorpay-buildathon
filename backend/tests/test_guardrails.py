"""
Covers every claim in DEMO_SCRIPT.md's guardrail/checkout scenarios,
split across the two rails:
  - Human rail (POST /checkout, Payment Links) -- guarded by
    guardrails.py (cart review + stock only).
  - Agent rail (POST /agent/pay, Orders API) -- guarded by policy.py's
    8-rule decision engine, self-captured immediately.
Plus the actor-spoof, idempotency, and policy-specific guarantees
(price-tamper, expired-warrant-at-pay-time, category, pay-without-
confirm, capture-then-revenue).

Runs entirely in mock mode (no Razorpay keys, no network) -- see
conftest.py.
"""

import json
import time
import uuid

from app import catalog, cart as cart_mod, sessions, webhooks

DEFAULT_CATEGORIES = ["electronics", "apparel", "home", "stationery"]


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------

def make_human_session(client) -> str:
    resp = client.post("/session/human")
    assert resp.status_code == 200
    return resp.json()["session_id"]


def make_agent_session(client, per_tx_cap_inr=2000, daily_cap_inr=5000,
                        allowed_categories=None, expires_in=3600, nonce=None,
                        secret=None):
    warrant = {
        "agent_id": "test_agent",
        "merchant_id": sessions.MERCHANT_ID,
        "per_tx_cap_inr": per_tx_cap_inr,
        "daily_cap_inr": daily_cap_inr,
        "allowed_categories": allowed_categories or DEFAULT_CATEGORIES,
        "expires_at": time.time() + expires_in,
        "nonce": nonce or uuid.uuid4().hex,
    }
    signature = sessions.sign_warrant(warrant, secret=secret)
    return client.post("/session/agent", json={"warrant": warrant, "signature": signature})


def agent_session_id(client, **kwargs) -> str:
    resp = make_agent_session(client, **kwargs)
    assert resp.status_code == 200, resp.text
    return resp.json()["session_id"]


def add_and_review(client, session_id, product_id, qty=1):
    resp = client.post("/cart/add", json={"session_id": session_id, "product_id": product_id, "qty": qty})
    assert resp.status_code == 200, resp.text
    view = client.get(f"/cart/{session_id}")
    assert view.status_code == 200
    return resp.json()


def new_idempotency_key() -> str:
    return uuid.uuid4().hex


def pay(client, session_id, idempotency_key=None, confirm=True):
    return client.post("/agent/pay", json={
        "session_id": session_id,
        "idempotency_key": idempotency_key or new_idempotency_key(),
        "confirm": confirm,
    })


def capture_human_order(client, payment_link_id, amount_inr):
    """Human-rail capture -- POST /demo/simulate-capture, signed with
    RAZORPAY_WEBHOOK_SECRET the same way a real Razorpay webhook call
    would be (see webhooks.build_capture_payload / sign_body)."""
    body = webhooks.build_capture_payload(payment_link_id=payment_link_id, amount_inr=amount_inr)
    body_bytes = json.dumps(body).encode("utf-8")
    signature = webhooks.sign_body(body_bytes)
    return client.post("/demo/simulate-capture", content=body_bytes,
                        headers={"X-Razorpay-Signature": signature, "Content-Type": "application/json"})


def last_checkout_payment_details(client, session_id):
    entries = client.get("/merchants/demo_merchant/audit-trail", params={"session_id": session_id}).json()["entries"]
    entry = next(e for e in entries if e["action"] == "checkout_payment")
    return json.loads(entry["details"])


# ---------------------------------------------------------------------
# Human rail -- scenario a, d, e (Payment Links)
# ---------------------------------------------------------------------

def test_human_happy_path_checkout_and_capture(client):
    session_id = make_human_session(client)
    add_and_review(client, session_id, "sku_001", qty=1)

    resp = client.post("/checkout", json={"session_id": session_id, "idempotency_key": new_idempotency_key()})
    assert resp.status_code == 200
    body = resp.json()
    assert body["payment_link"]
    assert body["amount_inr"] == 1499

    metrics = client.get("/metrics").json()
    assert metrics["captured_inr"] == 0.0
    assert metrics["orders_created_inr"] == 1499.0

    payment_link_id = last_checkout_payment_details(client, session_id)["payment_link_id"]
    cap_resp = capture_human_order(client, payment_link_id, 1499)
    assert cap_resp.status_code == 200

    metrics = client.get("/metrics").json()
    assert metrics["captured_inr"] == 1499.0
    assert metrics["total_revenue_inr"] == 1499.0


def test_human_simulate_failure_recovers_with_real_400_shape(client):
    session_id = make_human_session(client)
    add_and_review(client, session_id, "sku_003", qty=1)

    resp = client.post("/checkout", json={
        "session_id": session_id, "idempotency_key": new_idempotency_key(), "simulate_failure": True,
    })
    assert resp.status_code == 200
    assert "recovered_after_retry" in resp.json()["note"]

    details = last_checkout_payment_details(client, session_id)
    assert details["first_attempt_response"]["error"]["code"] == "BAD_REQUEST_ERROR"
    assert details["retry_attempt_response"] is not None


def test_human_gated_block_without_cart_review(client):
    session_id = make_human_session(client)
    resp = client.post("/cart/add", json={"session_id": session_id, "product_id": "sku_001", "qty": 1})
    assert resp.status_code == 200
    # Deliberately skip GET /cart/{session_id}.

    checkout_resp = client.post("/checkout", json={"session_id": session_id, "idempotency_key": new_idempotency_key()})
    assert checkout_resp.status_code == 403
    assert "cart_not_reviewed" in checkout_resp.json()["detail"]


def test_human_checkout_on_empty_cart_is_logged(client):
    """REGRESSION TEST -- a checkout attempt on an empty cart used to
    reject with 400 cart_empty WITHOUT ever writing an audit entry,
    even though the web chat's own UI unconditionally claims
    "[audit] failure logged" for any checkout failure. Every rejected
    money action must actually be explainable, not just claimed to be."""
    session_id = make_human_session(client)
    resp = client.post("/checkout", json={"session_id": session_id, "idempotency_key": new_idempotency_key()})
    assert resp.status_code == 400
    assert resp.json()["detail"] == "cart_empty"

    entries = client.get("/merchants/demo_merchant/audit-trail", params={"session_id": session_id}).json()["entries"]
    entry = next(e for e in entries if e["action"] == "checkout_attempt")
    assert entry["status"] == "failed"
    assert json.loads(entry["details"])["reason"] == "cart_empty"


def test_agent_pay_on_empty_cart_is_logged(client):
    session_id = agent_session_id(client)
    resp = pay(client, session_id)
    assert resp.status_code == 400
    assert resp.json()["detail"] == "cart_empty"

    entries = client.get("/merchants/demo_merchant/audit-trail", params={"session_id": session_id}).json()["entries"]
    entry = next(e for e in entries if e["action"] == "checkout_attempt")
    assert entry["status"] == "failed"
    assert json.loads(entry["details"])["reason"] == "cart_empty"


def test_human_session_cannot_use_agent_pay(client):
    session_id = make_human_session(client)
    add_and_review(client, session_id, "sku_001", qty=1)
    resp = pay(client, session_id)
    assert resp.status_code == 400
    assert "agent" in resp.json()["detail"].lower()


def test_agent_session_cannot_use_checkout(client):
    session_id = agent_session_id(client)
    add_and_review(client, session_id, "sku_001", qty=1)
    resp = client.post("/checkout", json={"session_id": session_id, "idempotency_key": new_idempotency_key()})
    assert resp.status_code == 400
    assert "agent" in resp.json()["detail"].lower()


# ---------------------------------------------------------------------
# Agent rail -- scenario b, c, f (Orders API, self-captured)
# ---------------------------------------------------------------------

def test_agent_out_of_stock_block(client):
    session_id = agent_session_id(client)
    add_and_review(client, session_id, "sku_005", qty=1)  # sku_005 has 0 stock

    resp = pay(client, session_id)
    assert resp.status_code == 403
    assert "out_of_stock" in resp.json()["detail"]


def test_agent_per_transaction_cap_block(client):
    session_id = agent_session_id(client, per_tx_cap_inr=2000)
    add_and_review(client, session_id, "sku_001", qty=2)  # 2998 > 2000 cap

    resp = pay(client, session_id)
    assert resp.status_code == 403
    assert "exceeds per-transaction cap of 2000" in resp.json()["detail"]


def test_agent_daily_cap_counts_captured_only(client):
    """POST /agent/pay self-captures synchronously, so each successful
    call immediately counts toward the daily cap -- three Rs.1,848
    transactions, each individually under the Rs.2,000 per-tx cap, the
    third blocked once 3,696 + 1,848 = 5,544 exceeds the Rs.5,000
    daily cap."""
    session_id = agent_session_id(client, per_tx_cap_inr=2000, daily_cap_inr=5000)

    def one_transaction():
        add_and_review(client, session_id, "sku_001", qty=1)
        add_and_review(client, session_id, "sku_003", qty=1)
        return pay(client, session_id)

    first = one_transaction()
    assert first.status_code == 200
    assert first.json()["status"] == "captured"

    second = one_transaction()
    assert second.status_code == 200
    assert second.json()["status"] == "captured"

    third = one_transaction()
    assert third.status_code == 403
    assert "daily_spending_cap_exceeded" in third.json()["detail"]


# ---------------------------------------------------------------------
# Task 1 -- actor is looked up from the session, never trusted from body
# ---------------------------------------------------------------------

def test_actor_spoof_from_agent_session_still_applies_policy_caps(client):
    """Posting actor=human_whatsapp in the JSON body of a cart/pay call
    must be silently ignored -- the real actor (ai_agent_mcp) is fixed
    by the session, so the per-transaction cap still applies."""
    session_id = agent_session_id(client, per_tx_cap_inr=2000)

    add_resp = client.post("/cart/add", json={
        "session_id": session_id, "product_id": "sku_001", "qty": 2,
        "actor": "human_whatsapp",  # spoof attempt -- must be ignored
    })
    assert add_resp.status_code == 200
    client.get(f"/cart/{session_id}")

    pay_resp = client.post("/agent/pay", json={
        "session_id": session_id, "idempotency_key": new_idempotency_key(), "confirm": True,
        "actor": "human_whatsapp",  # spoof attempt -- must be ignored
    })
    assert pay_resp.status_code == 403
    assert "exceeds per-transaction cap" in pay_resp.json()["detail"]


def test_cart_endpoints_require_a_valid_session(client):
    resp = client.post("/cart/add", json={"session_id": "not_a_real_session", "product_id": "sku_001", "qty": 1})
    assert resp.status_code == 401

    resp = client.get("/cart/not_a_real_session")
    assert resp.status_code == 401

    resp = client.post("/checkout", json={"session_id": "not_a_real_session", "idempotency_key": "x"})
    assert resp.status_code == 401

    resp = pay(client, "not_a_real_session")
    assert resp.status_code == 401


def test_agent_session_requires_valid_signature(client):
    resp = make_agent_session(client, secret="wrong_secret")
    assert resp.status_code == 401
    assert "signature_mismatch" in resp.json()["detail"]


def test_agent_session_rejects_expired_warrant_at_mint_time(client):
    resp = make_agent_session(client, expires_in=-10)
    assert resp.status_code == 401
    assert "warrant_expired" in resp.json()["detail"]


def test_agent_session_rejects_reused_nonce(client):
    nonce = uuid.uuid4().hex
    first = make_agent_session(client, nonce=nonce)
    assert first.status_code == 200

    second = make_agent_session(client, nonce=nonce)
    assert second.status_code == 401
    assert "nonce_reused_or_missing" in second.json()["detail"]


def test_agent_rejects_disallowed_category_at_pay(client):
    session_id = agent_session_id(client, allowed_categories=["stationery"])
    add_and_review(client, session_id, "sku_001", qty=1)  # electronics, not allowed

    resp = pay(client, session_id)
    assert resp.status_code == 403
    assert "category_not_allowed" in resp.json()["detail"]


# ---------------------------------------------------------------------
# New for this task -- policy.py rule coverage
# ---------------------------------------------------------------------

def test_price_tamper_block(client):
    """policy.py recomputes the total from the SERVER catalog and
    compares it against the cart's OWN stored line-item price (rule 4)
    -- if a cart entry's stored price no longer matches the live
    catalog (simulated here by directly corrupting the in-memory cart,
    standing in for any bypass of the normal add-to-cart path), pay()
    must block rather than trust either side blindly."""
    session_id = agent_session_id(client)
    add_and_review(client, session_id, "sku_001", qty=1)

    # Simulate a tampered/corrupted cart entry -- normal add_to_cart()
    # never lets a client set this value, this is deliberately
    # reaching past that to prove the server-side check works.
    cart_mod.set_line_item_price_for_tests(session_id, "sku_001", 1)

    resp = pay(client, session_id)
    assert resp.status_code == 403
    assert "price_tampered" in resp.json()["detail"]


def test_price_not_tampered_passes(client):
    """Sanity check for the test above -- an untouched cart must NOT
    trip the tamper check."""
    session_id = agent_session_id(client)
    add_and_review(client, session_id, "sku_001", qty=1)
    resp = pay(client, session_id)
    assert resp.status_code == 200


def test_expired_warrant_blocks_at_pay_time_not_just_mint_time(client):
    """A warrant valid when the session was minted can still expire
    before the agent actually calls pay() -- policy.py re-verifies
    expiry on every attempt, independent of sessions.py's mint-time
    check."""
    session_id = agent_session_id(client, expires_in=3600)
    add_and_review(client, session_id, "sku_001", qty=1)

    # Simulate time passing past the warrant's expiry, without a real
    # sleep -- overwrite the stored warrant (the same one policy.py
    # reads at pay time) AND re-sign it to match, so this isolates the
    # expiry rule from the signature rule (mutating the warrant without
    # re-signing would trip "invalid_warrant_signature" first instead).
    session = sessions.get_session(session_id)
    session["warrant"]["expires_at"] = time.time() - 1
    new_signature = sessions.sign_warrant(session["warrant"])
    sessions.set_warrant_for_tests(session_id, session["warrant"], new_signature)

    resp = pay(client, session_id)
    assert resp.status_code == 403
    assert "warrant_expired" in resp.json()["detail"]


def test_pay_without_confirm_blocks_and_creates_no_order(client):
    session_id = agent_session_id(client)
    add_and_review(client, session_id, "sku_001", qty=1)

    resp = pay(client, session_id, confirm=False)
    assert resp.status_code == 400

    # No order, no policy decision, no payment attempt of any kind.
    entries = client.get("/merchants/demo_merchant/audit-trail", params={"session_id": session_id}).json()["entries"]
    assert not any(e["action"] in ("policy_decision", "checkout_payment") for e in entries)


def test_capture_then_revenue(client):
    """POST /agent/pay self-captures synchronously (unlike the human
    rail's separate /demo/simulate-capture step) -- a single successful
    call must show up as captured revenue immediately, with no extra
    step required."""
    session_id = agent_session_id(client)
    add_and_review(client, session_id, "sku_003", qty=1)

    resp = pay(client, session_id)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "captured"
    assert body["razorpay_order_id"]

    metrics = client.get("/metrics").json()
    assert metrics["captured_inr"] == 349.0
    assert metrics["total_revenue_inr"] == 349.0
    assert metrics["revenue_by_actor"]["ai_agent_mcp"] == 349.0

    # Stock actually decremented too, same as the human rail's capture step.
    assert catalog.get_product("demo_merchant", "sku_003")["stock"] == 59


# ---------------------------------------------------------------------
# remaining_cap / explain_last_block endpoints (back MCP tools of the
# same names)
# ---------------------------------------------------------------------

def test_remaining_cap_reflects_captured_spend(client):
    session_id = agent_session_id(client, per_tx_cap_inr=2000, daily_cap_inr=5000)

    resp = client.get("/agent/remaining-cap", params={"session_id": session_id})
    assert resp.status_code == 200
    body = resp.json()
    assert body["per_tx_cap_inr"] == 2000
    assert body["daily_remaining_inr"] == 5000

    add_and_review(client, session_id, "sku_003", qty=1)
    pay_resp = pay(client, session_id)
    assert pay_resp.status_code == 200

    resp = client.get("/agent/remaining-cap", params={"session_id": session_id})
    assert resp.json()["daily_remaining_inr"] == 5000 - 349


def test_explain_last_block_returns_full_decision(client):
    session_id = agent_session_id(client, allowed_categories=["stationery"])
    add_and_review(client, session_id, "sku_001", qty=1)  # electronics -- blocked

    blocked = pay(client, session_id)
    assert blocked.status_code == 403

    resp = client.get("/agent/explain-last-block", params={"session_id": session_id})
    assert resp.status_code == 200
    decision = resp.json()["decision"]
    assert decision["allow"] is False
    assert "category_not_allowed" in decision["reason"]


# ---------------------------------------------------------------------
# Task 2 -- idempotency
# ---------------------------------------------------------------------

def test_double_submit_same_idempotency_key_returns_original_order_human(client):
    session_id = make_human_session(client)
    add_and_review(client, session_id, "sku_001", qty=1)
    key = new_idempotency_key()

    first = client.post("/checkout", json={"session_id": session_id, "idempotency_key": key})
    assert first.status_code == 200
    first_body = first.json()

    second = client.post("/checkout", json={"session_id": session_id, "idempotency_key": key})
    assert second.status_code == 200
    assert second.json() == first_body

    payment_entries = [e for e in client.get("/merchants/demo_merchant/audit-trail", params={"session_id": session_id}).json()["entries"]
                        if e["action"] == "checkout_payment"]
    assert len(payment_entries) == 1


def test_double_submit_same_idempotency_key_returns_original_order_agent(client):
    session_id = agent_session_id(client)
    add_and_review(client, session_id, "sku_001", qty=1)
    key = new_idempotency_key()

    first = pay(client, session_id, idempotency_key=key)
    assert first.status_code == 200
    first_body = first.json()

    second = pay(client, session_id, idempotency_key=key)
    assert second.status_code == 200
    assert second.json() == first_body

    payment_entries = [e for e in client.get("/merchants/demo_merchant/audit-trail", params={"session_id": session_id}).json()["entries"]
                        if e["action"] == "checkout_payment"]
    assert len(payment_entries) == 1

    # Only ONE capture -- stock decremented exactly once.
    assert catalog.get_product("demo_merchant", "sku_001")["stock"] == 24


def test_different_idempotency_key_same_session_creates_a_new_order(client):
    session_id = make_human_session(client)

    add_and_review(client, session_id, "sku_001", qty=1)
    first = client.post("/checkout", json={"session_id": session_id, "idempotency_key": new_idempotency_key()})
    assert first.status_code == 200

    add_and_review(client, session_id, "sku_003", qty=1)
    second = client.post("/checkout", json={"session_id": session_id, "idempotency_key": new_idempotency_key()})
    assert second.status_code == 200
    assert second.json()["order_id"] != first.json()["order_id"]


# ---------------------------------------------------------------------
# Task 3 -- stock is a per-line-item invariant, decremented at capture
# time only, restored if capture fails
# ---------------------------------------------------------------------

def test_qty_over_stock_blocks_even_when_stock_is_positive(client):
    """sku_001 has 25 in stock -- requesting 30 must block as
    out_of_stock even though 25 > 0. Uses a high per-tx cap so THAT
    rule doesn't fire first and mask the stock check being tested."""
    session_id = agent_session_id(client, per_tx_cap_inr=100_000, daily_cap_inr=100_000)
    add_and_review(client, session_id, "sku_001", qty=30)

    resp = pay(client, session_id)
    assert resp.status_code == 403
    assert "out_of_stock" in resp.json()["detail"]


def test_stock_not_decremented_until_capture(client):
    session_id = make_human_session(client)
    starting_stock = catalog.get_product("demo_merchant", "sku_001")["stock"]

    add_and_review(client, session_id, "sku_001", qty=1)
    resp = client.post("/checkout", json={"session_id": session_id, "idempotency_key": new_idempotency_key()})
    assert resp.status_code == 200

    assert catalog.get_product("demo_merchant", "sku_001")["stock"] == starting_stock  # unchanged -- not yet captured

    payment_link_id = last_checkout_payment_details(client, session_id)["payment_link_id"]
    capture_human_order(client, payment_link_id, 1499)

    assert catalog.get_product("demo_merchant", "sku_001")["stock"] == starting_stock - 1  # decremented only now


def test_capture_failure_restores_stock(client):
    """Simulates the race where stock drops below what an order needs
    between order-creation and capture time. The webhook call itself
    still returns 200 -- it was successfully verified and processed,
    same as a real Razorpay webhook ack -- but the CAPTURE inside it
    must fail cleanly (logged as payment_confirmed/capture_failed) and
    not leave stock partially decremented."""
    session_id = make_human_session(client)
    add_and_review(client, session_id, "sku_004", qty=5)  # stock 200, plenty at order time
    resp = client.post("/checkout", json={"session_id": session_id, "idempotency_key": new_idempotency_key()})
    assert resp.status_code == 200

    payment_link_id = last_checkout_payment_details(client, session_id)["payment_link_id"]

    catalog.set_stock_for_tests("demo_merchant", "sku_004", 2)

    cap_resp = capture_human_order(client, payment_link_id, 249 * 5)
    assert cap_resp.status_code == 200

    entries = client.get("/merchants/demo_merchant/audit-trail", params={"session_id": session_id}).json()["entries"]
    confirmed = next(e for e in entries if e["action"] == "payment_confirmed")
    assert confirmed["status"] == "capture_failed"
    assert "insufficient_stock" in json.loads(confirmed["details"])["reason"]


def test_agent_pay_reports_capture_failed_status_accurately(client, monkeypatch):
    """Regression test -- handle_webhook()'s "ok" reflects whether the
    webhook itself was verified/processed, not whether the capture
    inside it actually succeeded (a real Razorpay deployment acks a
    webhook the same way even if the merchant's own capture logic
    fails). /agent/pay must report the order's REAL status, and the
    replayed (idempotent) response must match -- not a stale
    "pending_capture" snapshot taken before capture ever ran."""
    session_id = agent_session_id(client)
    add_and_review(client, session_id, "sku_001", qty=1)
    key = new_idempotency_key()

    monkeypatch.setattr(catalog, "decrement_stock", lambda merchant_id, product_id, qty: False)

    resp = pay(client, session_id, idempotency_key=key)
    assert resp.status_code == 200
    assert resp.json()["status"] == "capture_failed"

    replay = pay(client, session_id, idempotency_key=key)
    assert replay.status_code == 200
    assert replay.json() == resp.json()  # stored response matches what was actually returned, not stale


# ---------------------------------------------------------------------
# Task 5 -- revenue is captured-only; orders_created_inr shows the gap
# ---------------------------------------------------------------------

def test_orders_created_vs_captured_gap(client):
    session_id = make_human_session(client)
    add_and_review(client, session_id, "sku_001", qty=1)
    resp = client.post("/checkout", json={"session_id": session_id, "idempotency_key": new_idempotency_key()})
    assert resp.status_code == 200

    metrics = client.get("/metrics").json()
    assert metrics["orders_created_inr"] == 1499.0
    assert metrics["captured_inr"] == 0.0
    assert metrics["total_revenue_inr"] == 0.0


# ---------------------------------------------------------------------
# Catalog is agent-readable
# ---------------------------------------------------------------------

def test_catalog_has_agent_readable_fields(client):
    resp = client.get("/catalog")
    assert resp.status_code == 200
    products = resp.json()["products"]
    assert products

    oos = next(p for p in products if p["id"] == "sku_005")
    in_stock = next(p for p in products if p["id"] == "sku_001")

    for p in (oos, in_stock):
        for field in ("sku", "name", "description", "price_inr", "currency", "tax_bps",
                       "stock", "availability", "category", "attributes", "return_window_days"):
            assert field in p, f"missing {field}"

    assert oos["availability"] == "out_of_stock"
    assert in_stock["availability"] == "in_stock"
    assert oos["sku"] == oos["id"]
