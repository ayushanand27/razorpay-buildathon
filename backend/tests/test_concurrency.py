"""
Concurrency safety -- proves the database-level idempotency claim
(orders.claim_idempotency_key(), a real PRIMARY KEY constraint --
see db.py's IdempotencyRecord) actually prevents the race it's meant
to close: two genuinely concurrent requests carrying the SAME
(session_id, idempotency_key) must never create two orders, on either
rail. This replaced an in-memory threading.Lock() dict that only ever
protected a single process -- a PRIMARY KEY constraint SQLite enforces
atomically holds even across separate worker processes.

Fires real concurrent requests against the SAME TestClient/app instance
from separate OS threads (a barrier makes both hit the claim at
essentially the same moment) -- this exercises the actual database
constraint, not just sequential calls that would pass even without one.
"""

import threading
import time
import uuid

from app import sessions

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


def _fire_concurrently(client, method, path, bodies):
    """Fires len(bodies) requests at path from separate threads,
    released at the same instant via a barrier, and returns their
    responses in completion order (not necessarily submission order)."""
    barrier = threading.Barrier(len(bodies))
    results = []
    results_lock = threading.Lock()

    def worker(body):
        barrier.wait()
        resp = getattr(client, method)(path, json=body)
        with results_lock:
            results.append(resp)

    threads = [threading.Thread(target=worker, args=(b,)) for b in bodies]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results


def test_concurrent_checkout_same_idempotency_key_creates_only_one_order(client):
    session_id = make_human_session(client)
    add_and_review(client, session_id, "sku_001", qty=1)
    key = "concurrent-checkout-key"

    responses = _fire_concurrently(
        client, "post", "/checkout",
        [{"session_id": session_id, "idempotency_key": key}] * 2,
    )

    assert all(r.status_code == 200 for r in responses)
    assert responses[0].json() == responses[1].json()

    entries = client.get("/merchants/demo_merchant/audit-trail", params={"session_id": session_id}).json()["entries"]
    payment_entries = [e for e in entries if e["action"] == "checkout_payment"]
    assert len(payment_entries) == 1, "concurrent duplicate requests must produce exactly one order"


def test_concurrent_agent_pay_same_idempotency_key_creates_only_one_order(client):
    from app import catalog

    session_id = agent_session_id(client)
    add_and_review(client, session_id, "sku_003", qty=1)
    key = "concurrent-agent-pay-key"
    starting_stock = catalog.get_product("demo_merchant", "sku_003")["stock"]

    responses = _fire_concurrently(
        client, "post", "/agent/pay",
        [{"session_id": session_id, "idempotency_key": key, "confirm": True}] * 2,
    )

    assert all(r.status_code == 200 for r in responses)
    assert responses[0].json() == responses[1].json()

    entries = client.get("/merchants/demo_merchant/audit-trail", params={"session_id": session_id}).json()["entries"]
    payment_entries = [e for e in entries if e["action"] == "checkout_payment"]
    assert len(payment_entries) == 1

    # Stock decremented exactly once, not twice -- proves capture_order's
    # own atomic conditional UPDATE (status='created' -> 'capturing')
    # also holds even if two callers somehow both reached it.
    assert catalog.get_product("demo_merchant", "sku_003")["stock"] == starting_stock - 1


def test_concurrent_requests_different_sessions_both_succeed_independently(client):
    """The per-session lock must NOT serialize unrelated buyers -- two
    different sessions checking out concurrently both succeed as their
    own, separate orders."""
    session_a = make_human_session(client)
    session_b = make_human_session(client)
    add_and_review(client, session_a, "sku_001", qty=1)
    add_and_review(client, session_b, "sku_003", qty=1)

    barrier = threading.Barrier(2)
    results = {}

    def worker(name, session_id, key):
        barrier.wait()
        resp = client.post("/checkout", json={"session_id": session_id, "idempotency_key": key})
        results[name] = resp

    t1 = threading.Thread(target=worker, args=("a", session_a, "key-a"))
    t2 = threading.Thread(target=worker, args=("b", session_b, "key-b"))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert results["a"].status_code == 200
    assert results["b"].status_code == 200
    assert results["a"].json()["order_id"] != results["b"].json()["order_id"]
