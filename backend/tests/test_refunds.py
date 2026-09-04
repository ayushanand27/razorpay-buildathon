"""
Covers POST /refund -- reverses a captured order on either rail,
restores stock, and shows up honestly in metrics (refunded_inr /
net_revenue_inr) rather than silently vanishing from captured_inr.
"""

import time
import uuid

from app import catalog, sessions

DEFAULT_CATEGORIES = ["electronics", "apparel", "home", "stationery"]


def make_human_session(client) -> str:
    resp = client.post("/session/human")
    assert resp.status_code == 200
    return resp.json()["session_id"]


def agent_session_id(client, per_tx_cap_inr=5000, daily_cap_inr=5000) -> str:
    warrant = {
        "agent_id": "test_agent",
        "merchant_id": sessions.MERCHANT_ID,
        "per_tx_cap_inr": per_tx_cap_inr,
        "daily_cap_inr": daily_cap_inr,
        "allowed_categories": DEFAULT_CATEGORIES,
        "expires_at": time.time() + 3600,
        "nonce": uuid.uuid4().hex,
    }
    signature = sessions.sign_warrant(warrant)
    resp = client.post("/session/agent", json={"warrant": warrant, "signature": signature})
    assert resp.status_code == 200, resp.text
    return resp.json()["session_id"]


def add_and_review(client, session_id, product_id, qty=1):
    resp = client.post("/cart/add", json={"session_id": session_id, "product_id": product_id, "qty": qty})
    assert resp.status_code == 200, resp.text
    client.get(f"/cart/{session_id}")


def capture_human_order(client, payment_link_id, amount_inr):
    from app import webhooks
    import json as json_module
    body = webhooks.build_capture_payload(payment_link_id=payment_link_id, amount_inr=amount_inr)
    body_bytes = json_module.dumps(body).encode("utf-8")
    signature = webhooks.sign_body(body_bytes)
    return client.post("/demo/simulate-capture", content=body_bytes,
                        headers={"X-Razorpay-Signature": signature, "Content-Type": "application/json"})


def test_refund_agent_order_restores_stock_and_metrics(client):
    session_id = agent_session_id(client)
    add_and_review(client, session_id, "sku_001", qty=1)
    starting_stock = catalog.get_product("sku_001")["stock"]

    pay_resp = client.post("/agent/pay", json={
        "session_id": session_id, "idempotency_key": "refund-test-1", "confirm": True,
    })
    assert pay_resp.status_code == 200
    order_id = pay_resp.json()["order_id"]
    assert catalog.get_product("sku_001")["stock"] == starting_stock - 1

    metrics = client.get("/metrics").json()
    assert metrics["captured_inr"] == 1499.0
    assert metrics["refunded_inr"] == 0.0
    assert metrics["net_revenue_inr"] == 1499.0

    refund_resp = client.post("/refund", json={"session_id": session_id, "order_id": order_id})
    assert refund_resp.status_code == 200
    assert refund_resp.json()["status"] == "refunded"

    assert catalog.get_product("sku_001")["stock"] == starting_stock  # restored

    metrics = client.get("/metrics").json()
    assert metrics["captured_inr"] == 1499.0  # gross captured stays historically accurate
    assert metrics["refunded_inr"] == 1499.0
    assert metrics["net_revenue_inr"] == 0.0


def test_refund_human_order_via_payment_link_rail(client):
    session_id = make_human_session(client)
    add_and_review(client, session_id, "sku_003", qty=1)
    checkout_resp = client.post("/checkout", json={"session_id": session_id, "idempotency_key": "refund-human-1"})
    assert checkout_resp.status_code == 200
    order_id = checkout_resp.json()["order_id"]

    entries = client.get("/audit-trail").json()["entries"]
    import json as json_module
    payment_link_id = next(
        json_module.loads(e["details"])["payment_link_id"] for e in entries if e["action"] == "checkout_payment"
    )
    cap_resp = capture_human_order(client, payment_link_id, 349)
    assert cap_resp.status_code == 200

    refund_resp = client.post("/refund", json={"session_id": session_id, "order_id": order_id})
    assert refund_resp.status_code == 200
    assert refund_resp.json()["status"] == "refunded"


def test_refund_uncaptured_order_blocked(client):
    session_id = make_human_session(client)
    add_and_review(client, session_id, "sku_001", qty=1)
    checkout_resp = client.post("/checkout", json={"session_id": session_id, "idempotency_key": "refund-uncaptured"})
    order_id = checkout_resp.json()["order_id"]

    resp = client.post("/refund", json={"session_id": session_id, "order_id": order_id})
    assert resp.status_code == 400
    assert "order_not_captured" in resp.json()["detail"]


def test_refund_wrong_session_blocked(client):
    session_a = agent_session_id(client)
    session_b = agent_session_id(client)
    add_and_review(client, session_a, "sku_001", qty=1)
    pay_resp = client.post("/agent/pay", json={
        "session_id": session_a, "idempotency_key": "refund-wrong-session", "confirm": True,
    })
    order_id = pay_resp.json()["order_id"]

    resp = client.post("/refund", json={"session_id": session_b, "order_id": order_id})
    assert resp.status_code == 403


def test_refund_is_idempotent(client):
    session_id = agent_session_id(client)
    add_and_review(client, session_id, "sku_001", qty=1)
    pay_resp = client.post("/agent/pay", json={
        "session_id": session_id, "idempotency_key": "refund-idempotent", "confirm": True,
    })
    order_id = pay_resp.json()["order_id"]
    starting_stock = catalog.get_product("sku_001")["stock"]

    first = client.post("/refund", json={"session_id": session_id, "order_id": order_id})
    assert first.status_code == 200
    stock_after_first = catalog.get_product("sku_001")["stock"]

    second = client.post("/refund", json={"session_id": session_id, "order_id": order_id})
    assert second.status_code == 200

    # Stock restored exactly once, not twice.
    assert catalog.get_product("sku_001")["stock"] == stock_after_first == starting_stock + 1


def test_refund_missing_order_404(client):
    session_id = make_human_session(client)
    resp = client.post("/refund", json={"session_id": session_id, "order_id": "order_does_not_exist"})
    assert resp.status_code == 404
