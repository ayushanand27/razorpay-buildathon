"""
POST /webhook/razorpay -- fed RAW, unparsed JSON bytes shaped exactly
like a real Razorpay webhook delivery (not run through
webhooks.build_capture_payload(), except where a test explicitly says
so), via FastAPI's TestClient, the same way an actual Razorpay
delivery would arrive over HTTP. Covers the three failure modes a
production webhook receiver has to get right:

  1. invalid/missing signature -- the body was never actually signed
     by (or was tampered with after) Razorpay.
  2. expired/missing timestamp -- an HMAC signature alone never
     expires, so a captured, validly-signed webhook replayed later
     must still be rejected on `created_at` freshness alone.
  3. replay -- even a webhook replayed INSIDE the freshness window
     must not double-capture an order or double-count revenue.

RAZORPAY_WEBHOOK_SECRET is fixed to "test_webhook_secret_do_not_use_in_prod"
by conftest.py.
"""

import hashlib
import hmac
import json
import time
import uuid

WEBHOOK_SECRET = "test_webhook_secret_do_not_use_in_prod"


def _sign(body_bytes: bytes, secret: str = WEBHOOK_SECRET) -> str:
    return hmac.new(secret.encode("utf-8"), body_bytes, hashlib.sha256).hexdigest()


def _post_webhook(client, body_bytes: bytes, signature: str):
    return client.post("/webhook/razorpay", content=body_bytes,
                        headers={"X-Razorpay-Signature": signature, "Content-Type": "application/json"})


def _real_shaped_payment_captured_payload(*, order_id: str | None = None, payment_link_id: str | None = None,
                                           amount_paise: int = 149900, created_at: int | None = None) -> dict:
    """A hand-built payload matching Razorpay's REAL payment.captured
    webhook schema (top-level entity/account_id/event/contains/
    created_at, a full nested payload.payment.entity block with the
    fields Razorpay actually sends) -- deliberately NOT built via
    webhooks.build_capture_payload(), so these tests don't just
    validate the app's own helper round-tripping through itself."""
    payment_entity = {
        "id": f"pay_{uuid.uuid4().hex[:14]}",
        "entity": "payment",
        "amount": amount_paise,
        "currency": "INR",
        "status": "captured",
        "method": "upi",
        "captured": True,
        "description": "Agentic Commerce Demo order",
        "email": "buyer@example.com",
        "contact": "+919876543210",
        "fee": int(amount_paise * 0.02),
        "tax": 0,
        "notes": {},
        "created_at": created_at if created_at is not None else int(time.time()),
    }
    if order_id:
        payment_entity["order_id"] = order_id

    payload = {"payment": {"entity": payment_entity}}
    if payment_link_id:
        payload["payment_link"] = {"entity": {"id": payment_link_id, "entity": "payment_link"}}

    return {
        "entity": "event",
        "account_id": "acc_DemoAccount000",
        "event": "payment.captured",
        "contains": ["payment"],
        "payload": payload,
        "created_at": created_at if created_at is not None else int(time.time()),
    }


# ---------------------------------------------------------------------
# Helpers to get a real, capturable order on either rail (idempotency
# keys are unique per test so they never collide with each other).
# ---------------------------------------------------------------------

def _human_order(client) -> tuple[str, str, float]:
    """Returns (session_id, payment_link_id, amount_inr) for a
    Payment-Links-rail order created but NOT yet captured."""
    session_id = client.post("/session/human").json()["session_id"]
    client.post("/cart/add", json={"session_id": session_id, "product_id": "sku_003", "qty": 1})  # Rs.349
    client.get(f"/cart/{session_id}")
    resp = client.post("/checkout", json={"session_id": session_id, "idempotency_key": uuid.uuid4().hex})
    assert resp.status_code == 200, resp.text
    entries = client.get("/audit-trail", params={"session_id": session_id}).json()["entries"]
    details = next(json.loads(e["details"]) for e in entries if e["action"] == "checkout_payment")
    return session_id, details["payment_link_id"], 349.0


# ---------------------------------------------------------------------
# 1. Invalid / missing signature
# ---------------------------------------------------------------------

def test_webhook_invalid_signature_rejected(client):
    _session_id, payment_link_id, amount = _human_order(client)
    payload = _real_shaped_payment_captured_payload(payment_link_id=payment_link_id, amount_paise=int(amount * 100))
    body_bytes = json.dumps(payload).encode("utf-8")

    resp = _post_webhook(client, body_bytes, signature="0" * 64)  # well-formed hex, wrong value
    assert resp.status_code == 400
    assert resp.json()["detail"] == "invalid_signature"


def test_webhook_missing_signature_header_rejected(client):
    payload = _real_shaped_payment_captured_payload(payment_link_id="plink_doesnotexist")
    body_bytes = json.dumps(payload).encode("utf-8")

    resp = client.post("/webhook/razorpay", content=body_bytes, headers={"Content-Type": "application/json"})
    assert resp.status_code == 400
    assert resp.json()["detail"] == "invalid_signature"


def test_webhook_tampered_body_after_signing_rejected(client):
    """Signature is computed over the ORIGINAL bytes; any byte-level
    tamper afterward (e.g. an attacker bumping the amount) must be
    caught -- this is the entire point of signing the raw body rather
    than a re-serialized/re-parsed version of it."""
    payload = _real_shaped_payment_captured_payload(payment_link_id="plink_doesnotexist", amount_paise=10000)
    body_bytes = json.dumps(payload).encode("utf-8")
    sig = _sign(body_bytes)

    tampered = body_bytes.replace(b'"amount": 10000', b'"amount": 9999900')
    assert tampered != body_bytes

    resp = _post_webhook(client, tampered, sig)
    assert resp.status_code == 400
    assert resp.json()["detail"] == "invalid_signature"


# ---------------------------------------------------------------------
# 2. Timestamp freshness (expired / missing)
# ---------------------------------------------------------------------

def test_webhook_expired_timestamp_rejected_even_with_valid_signature(client):
    """A stale webhook (created_at an hour ago) with an otherwise
    PERFECTLY valid signature must still be rejected -- proves the
    freshness check is a real, independent second layer, not
    redundant with signature verification."""
    stale_created_at = int(time.time()) - 3600
    payload = _real_shaped_payment_captured_payload(payment_link_id="plink_doesnotexist", created_at=stale_created_at)
    body_bytes = json.dumps(payload).encode("utf-8")
    sig = _sign(body_bytes)  # genuinely, correctly signed

    resp = _post_webhook(client, body_bytes, sig)
    assert resp.status_code == 400
    assert resp.json()["detail"] == "expired_timestamp"


def test_webhook_future_timestamp_beyond_clock_skew_rejected(client):
    future_created_at = int(time.time()) + 3600
    payload = _real_shaped_payment_captured_payload(payment_link_id="plink_doesnotexist", created_at=future_created_at)
    body_bytes = json.dumps(payload).encode("utf-8")
    sig = _sign(body_bytes)

    resp = _post_webhook(client, body_bytes, sig)
    assert resp.status_code == 400
    assert resp.json()["detail"] == "expired_timestamp"


def test_webhook_missing_created_at_rejected(client):
    """A real Razorpay webhook always carries created_at -- a payload
    shaped like one but missing it entirely is treated as invalid, not
    silently allowed through with no freshness check at all."""
    payload = _real_shaped_payment_captured_payload(payment_link_id="plink_doesnotexist")
    del payload["created_at"]
    body_bytes = json.dumps(payload).encode("utf-8")
    sig = _sign(body_bytes)

    resp = _post_webhook(client, body_bytes, sig)
    assert resp.status_code == 400
    assert resp.json()["detail"] == "missing_or_invalid_timestamp"


def test_webhook_unparseable_body_with_valid_signature_rejected(client):
    """Even a byte string that legitimately HMACs correctly (e.g. an
    attacker who somehow got the receiver to sign arbitrary bytes) must
    still be rejected if it isn't valid JSON."""
    body_bytes = b"not actually json{{{"
    sig = _sign(body_bytes)

    resp = _post_webhook(client, body_bytes, sig)
    assert resp.status_code == 400
    assert resp.json()["detail"] == "invalid_payload"


# ---------------------------------------------------------------------
# 3. Real capture, on both rails, from a raw hand-built payload
# ---------------------------------------------------------------------

def test_webhook_real_payload_captures_human_rail_order(client):
    session_id, payment_link_id, amount = _human_order(client)
    payload = _real_shaped_payment_captured_payload(payment_link_id=payment_link_id, amount_paise=int(amount * 100))
    body_bytes = json.dumps(payload).encode("utf-8")
    sig = _sign(body_bytes)

    resp = _post_webhook(client, body_bytes, sig)
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}

    entries = client.get("/audit-trail", params={"session_id": session_id}).json()["entries"]
    confirmed = next(e for e in entries if e["action"] == "payment_confirmed")
    assert confirmed["status"] == "paid"


def test_webhook_real_payload_for_unmatched_order_still_acks_200(client):
    """A webhook for an order/payment_link this backend never created
    (e.g. arriving after a database reset, or for a different
    deployment) must still ack 200 -- exactly what a real Razorpay
    integration is expected to do -- but log it as unmatched, not
    silently pretend it captured something."""
    payload = _real_shaped_payment_captured_payload(payment_link_id="plink_never_existed")
    body_bytes = json.dumps(payload).encode("utf-8")
    sig = _sign(body_bytes)

    resp = _post_webhook(client, body_bytes, sig)
    assert resp.status_code == 200


# ---------------------------------------------------------------------
# 4. Replay attack -- same valid, FRESH webhook delivered twice
# ---------------------------------------------------------------------

def test_webhook_replay_within_freshness_window_does_not_double_capture(client):
    from app import metrics

    session_id, payment_link_id, amount = _human_order(client)
    payload = _real_shaped_payment_captured_payload(payment_link_id=payment_link_id, amount_paise=int(amount * 100))
    body_bytes = json.dumps(payload).encode("utf-8")
    sig = _sign(body_bytes)

    before = metrics.get_metrics("demo_merchant")["captured_inr"]

    first = _post_webhook(client, body_bytes, sig)
    second = _post_webhook(client, body_bytes, sig)  # attacker (or Razorpay itself) resends the exact same delivery
    assert first.status_code == 200
    assert second.status_code == 200  # still acked -- a real Razorpay deployment expects that

    after = metrics.get_metrics("demo_merchant")["captured_inr"]
    assert after == before + amount, "replay must not double-count captured revenue"

    entries = client.get("/audit-trail", params={"session_id": session_id}).json()["entries"]
    confirmations = [e for e in entries if e["action"] == "payment_confirmed"]
    assert sum(1 for e in confirmations if e["status"] == "paid") == 1
    assert sum(1 for e in confirmations if e["status"] == "replayed") == 1


def test_webhook_replay_does_not_re_decrement_stock(client):
    from app import catalog

    stock_before = catalog.get_product("demo_merchant", "sku_003")["stock"]
    session_id, payment_link_id, amount = _human_order(client)  # decremented only at capture, not here
    payload = _real_shaped_payment_captured_payload(payment_link_id=payment_link_id, amount_paise=int(amount * 100))
    body_bytes = json.dumps(payload).encode("utf-8")
    sig = _sign(body_bytes)

    _post_webhook(client, body_bytes, sig)
    _post_webhook(client, body_bytes, sig)
    _post_webhook(client, body_bytes, sig)

    assert catalog.get_product("demo_merchant", "sku_003")["stock"] == stock_before - 1
