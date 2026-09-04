"""
Razorpay payment confirmation -- the step that actually decrements
stock and counts as captured revenue (see orders.py / metrics.py). A
payment link or order existing (payments.py / checkout() / agent_pay())
is NOT proof money moved; this module is what makes that final
"explainable" step real, by verifying an HMAC-SHA256 signature over
the raw request body, then a freshness check on the event's own
`created_at`, before trusting anything else in it.

There is exactly ONE verification+capture code path -- handle_webhook()
below -- used by BOTH:
  - POST /webhook/razorpay -- where a real Razorpay deployment would
    actually call, verified against RAZORPAY_WEBHOOK_SECRET.
  - POST /demo/simulate-capture -- this demo's stand-in, since there's
    no public URL for Razorpay to call locally. Signed with the SAME
    secret, verified by the SAME function, so "the webhook path is the
    same as production" is literally true, not just structurally
    similar. POST /agent/pay (the agent-checkout rail) also calls this
    same function directly (in-process, no network hairpin) to
    self-complete a purchase immediately after creating the order,
    since an AI agent has no browser to actually pay a real order in.

Unlike the Razorpay API keys (which default to blank -> mock mode),
RAZORPAY_WEBHOOK_SECRET is REQUIRED, matching AGENT_WARRANT_SECRET --
there is no "skip verification" fallback for either signed action in
this system.

Replay defense is two layers, matching what a real signed webhook
actually gives you:
  1. `created_at` freshness -- an HMAC signature alone never expires,
     so a captured (valid signature, valid body) webhook replayed
     minutes/hours/days later is rejected outright as stale, before
     it ever reaches order-matching logic.
  2. Even a replay inside the freshness window is a no-op, not a
     double-capture -- capture_order() is a one-way created->captured
     state transition (see orders.py); a webhook for an
     already-captured order is logged as `replayed`, not `paid`, so it
     can never inflate metrics.py's revenue totals or re-decrement
     stock, no matter how many times the same event is delivered.
"""

import hashlib
import hmac
import json
import os
import time
from pathlib import Path

from dotenv import load_dotenv

from . import audit, orders as orders_mod

# .env lives at the project root, one level above backend/ -- same
# loading pattern as payments.py.
load_dotenv(Path(__file__).resolve().parents[2] / ".env")

RAZORPAY_WEBHOOK_SECRET = os.environ.get("RAZORPAY_WEBHOOK_SECRET", "")

# Razorpay's own webhook docs recommend validating recency; 5 minutes
# is a generous window for real network/processing delay while still
# rejecting a captured-and-replayed-later event. A small negative
# tolerance also catches an event claiming to be from the future.
WEBHOOK_MAX_AGE_SECONDS = int(os.environ.get("WEBHOOK_MAX_AGE_SECONDS", "300"))
WEBHOOK_CLOCK_SKEW_SECONDS = 60


def sign_body(body_bytes: bytes, secret: str | None = None) -> str:
    """HMAC-SHA256 of the raw body bytes -- Razorpay's actual webhook
    signing scheme. Used to sign /demo/simulate-capture requests with
    the same algorithm a real Razorpay webhook call would use."""
    secret = RAZORPAY_WEBHOOK_SECRET if secret is None else secret
    return hmac.new(secret.encode("utf-8"), body_bytes, hashlib.sha256).hexdigest()


def _verify_signature(body: bytes, signature: str, secret: str) -> bool:
    """Prefers the razorpay SDK's own verifier when it's installed;
    falls back to a plain HMAC-SHA256 comparison otherwise -- the SDK
    is optional, the fallback is not."""
    try:
        import razorpay
        razorpay.Client(auth=("", "")).utility.verify_webhook_signature(
            body.decode("utf-8"), signature, secret
        )
        return True
    except ImportError:
        expected = sign_body(body, secret)
        return hmac.compare_digest(expected, signature or "")
    except Exception:
        # razorpay.errors.SignatureVerificationError (or any other
        # failure inside the SDK's own check) -- treat as invalid.
        return False


def _capture_and_log(order: dict, source: str, payment_id: str | None = None) -> tuple[bool, str | None]:
    ok, reason = orders_mod.capture_order(order["order_id"], payment_id=payment_id)

    if ok and reason == "already_captured":
        # A duplicate delivery of a webhook we already fully processed
        # -- log it as `replayed`, NOT `paid`, so metrics.py's revenue
        # sum (which totals every `paid` row) can never double-count a
        # single real payment just because Razorpay (or an attacker who
        # captured the request off the wire) sent the same event twice.
        audit.log_action(order["actor"], order["session_id"], "payment_confirmed", "replayed",
                          merchant_id=order["merchant_id"], amount_inr=order["total_inr"],
                          details={"payment_link_id": order["payment_link_id"],
                                   "razorpay_order_id": order["razorpay_order_id"],
                                   "source": source, "order_id": order["order_id"],
                                   "reason": "duplicate_webhook_delivery"})
        return True, reason

    if ok:
        audit.log_action(order["actor"], order["session_id"], "payment_confirmed", "paid",
                          merchant_id=order["merchant_id"], amount_inr=order["total_inr"],
                          details={"payment_link_id": order["payment_link_id"],
                                   "razorpay_order_id": order["razorpay_order_id"],
                                   "source": source, "order_id": order["order_id"]})
    else:
        audit.log_action(order["actor"], order["session_id"], "payment_confirmed", "capture_failed",
                          merchant_id=order["merchant_id"], amount_inr=order["total_inr"],
                          details={"payment_link_id": order["payment_link_id"],
                                   "razorpay_order_id": order["razorpay_order_id"],
                                   "source": source, "reason": reason, "order_id": order["order_id"]})
    return ok, reason


def handle_webhook(body: bytes, signature: str, source: str = "webhook") -> dict:
    """Returns {"ok": True} on a verified, fresh event, or {"ok": False,
    "reason": ...} on an invalid/unverifiable/stale one. Matches an
    order by payment_link_id (human/Payment-Links rail) or by the
    payment's order_id (agent/Orders rail) -- whichever the payload
    carries."""
    if not RAZORPAY_WEBHOOK_SECRET:
        return {"ok": False, "reason": "razorpay_webhook_secret_not_configured"}

    if not _verify_signature(body, signature, RAZORPAY_WEBHOOK_SECRET):
        audit.log_action("razorpay_webhook", "unknown", "webhook_received", "invalid_signature",
                          details={"reason": "signature_mismatch_or_missing", "source": source})
        return {"ok": False, "reason": "invalid_signature"}

    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        audit.log_action("razorpay_webhook", "unknown", "webhook_received", "invalid_signature",
                          details={"reason": "unparseable_body", "source": source})
        return {"ok": False, "reason": "invalid_payload"}

    created_at = payload.get("created_at")
    if not isinstance(created_at, (int, float)):
        audit.log_action("razorpay_webhook", "unknown", "webhook_received", "expired_timestamp",
                          details={"reason": "missing_or_invalid_created_at", "source": source})
        return {"ok": False, "reason": "missing_or_invalid_timestamp"}

    age = time.time() - created_at
    if age > WEBHOOK_MAX_AGE_SECONDS or age < -WEBHOOK_CLOCK_SKEW_SECONDS:
        audit.log_action("razorpay_webhook", "unknown", "webhook_received", "expired_timestamp",
                          details={"reason": "stale_or_future_timestamp", "created_at": created_at,
                                   "age_seconds": age, "source": source})
        return {"ok": False, "reason": "expired_timestamp"}

    event = payload.get("event", "")
    payment_entity = payload.get("payload", {}).get("payment", {}).get("entity", {})
    audit.log_action("razorpay_webhook", payment_entity.get("id", "unknown"), "webhook_received",
                      "verified", details={"event": event, "source": source})

    if event == "payment.captured":
        payment_link_id = (
            payload.get("payload", {}).get("payment_link", {}).get("entity", {}).get("id")
        )
        razorpay_order_id = payment_entity.get("order_id")

        order = None
        if payment_link_id:
            order = orders_mod.find_by_payment_link(payment_link_id)
        elif razorpay_order_id:
            order = orders_mod.find_by_razorpay_order_id(razorpay_order_id)

        if order:
            _capture_and_log(order, source, payment_id=payment_entity.get("id"))
        else:
            audit.log_action("razorpay_webhook", payment_link_id or razorpay_order_id or "unknown",
                              "payment_confirmed", "unmatched",
                              details={"reason": "no_matching_order", "payment_link_id": payment_link_id,
                                       "razorpay_order_id": razorpay_order_id, "source": source})

    return {"ok": True}


def build_capture_payload(*, payment_link_id: str | None = None, razorpay_order_id: str | None = None,
                           amount_inr: float = 0) -> dict:
    """Builds a payment.captured event shaped like a REAL Razorpay
    webhook payload (top-level entity/account_id/event/contains/
    created_at, plus the nested payload.payment.entity block) -- used
    by /demo/simulate-capture callers (tests, DEMO_SCRIPT, and POST
    /agent/pay's self-completion) so the exact same handle_webhook()
    parsing above, including the created_at freshness check, applies
    identically to both a real webhook and this demo stand-in."""
    payment_entity = {
        "id": f"pay_DEMO{os.urandom(5).hex()}",
        "amount": int(amount_inr * 100),
        "currency": "INR",
        "status": "captured",
    }
    if razorpay_order_id:
        payment_entity["order_id"] = razorpay_order_id

    payload = {"payment": {"entity": payment_entity}}
    if payment_link_id:
        payload["payment_link"] = {"entity": {"id": payment_link_id}}

    return {
        "entity": "event",
        "account_id": "acc_DEMO0000000000",
        "event": "payment.captured",
        "contains": ["payment"],
        "payload": payload,
        "created_at": int(time.time()),
    }
