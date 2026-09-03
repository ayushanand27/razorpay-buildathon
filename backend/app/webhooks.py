"""
Razorpay webhook handling -- confirms a payment was actually
*completed*, not just that a payment link was created. A payment link
existing (payments.py) is not proof money moved; this module is what
makes that final "explainable" step real, by verifying the
X-Razorpay-Signature HMAC before trusting anything in the payload.
"""

import hashlib
import hmac
import json
import os
from pathlib import Path

from dotenv import load_dotenv

from . import audit

# .env lives at the project root, one level above backend/ -- same
# loading pattern as payments.py.
load_dotenv(Path(__file__).resolve().parents[2] / ".env")

RAZORPAY_WEBHOOK_SECRET = os.environ.get("RAZORPAY_WEBHOOK_SECRET", "")


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
        expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, signature or "")
    except Exception:
        # razorpay.errors.SignatureVerificationError (or any other
        # failure inside the SDK's own check) -- treat as invalid.
        return False


def _find_session_for_payment_link(payment_link_id: str):
    """Linear-scan recent checkout_payment entries for the one whose
    logged payment_link_id matches. The audit trail doesn't index this
    -- a demo's traffic volume doesn't need it to."""
    for entry in audit.get_trail(limit=500):
        if entry["action"] != "checkout_payment":
            continue
        details = json.loads(entry["details"] or "{}")
        if details.get("payment_link_id") == payment_link_id:
            return entry
    return None


def handle_webhook(body: bytes, signature: str) -> dict:
    """Returns {"ok": True} on a verified (or mock) event, or
    {"ok": False, "reason": ...} on an invalid/unverifiable one --
    the caller maps that straight to a 200 or 400 response."""
    if not RAZORPAY_WEBHOOK_SECRET:
        # MOCK MODE -- no webhook secret configured, so there's nothing
        # real to verify. Log a clearly-labeled mock confirmation, same
        # pattern as payments.py's mock payment-link response, so the
        # "payment actually completed" step still shows up end to end.
        audit.log_action("razorpay_webhook", "mock", "payment_confirmed", "ok",
                          details={"mock": True, "note": "RAZORPAY_WEBHOOK_SECRET not set -- mock confirmation, signature not verified"})
        return {"ok": True}

    if not _verify_signature(body, signature, RAZORPAY_WEBHOOK_SECRET):
        audit.log_action("razorpay_webhook", "unknown", "webhook_received", "invalid_signature",
                          details={"reason": "signature_mismatch_or_missing"})
        return {"ok": False, "reason": "invalid_signature"}

    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        audit.log_action("razorpay_webhook", "unknown", "webhook_received", "invalid_signature",
                          details={"reason": "unparseable_body"})
        return {"ok": False, "reason": "invalid_payload"}

    event = payload.get("event", "")
    payment_entity = payload.get("payload", {}).get("payment", {}).get("entity", {})
    audit.log_action("razorpay_webhook", payment_entity.get("id", "unknown"),
                      "webhook_received", "verified", details={"event": event})

    if event == "payment.captured":
        payment_link_id = (
            payload.get("payload", {}).get("payment_link", {}).get("entity", {}).get("id")
        )
        matched = _find_session_for_payment_link(payment_link_id) if payment_link_id else None
        if matched:
            # Append-only trail, same as everywhere else in this system
            # (checkout_attempt -> checkout_payment already works this
            # way) -- record the confirmation as a new entry rather than
            # mutating the original checkout_payment row.
            audit.log_action(matched["actor"], matched["actor_id"], "payment_confirmed", "paid",
                              amount_inr=matched["amount_inr"],
                              details={"payment_link_id": payment_link_id, "event": event})
        else:
            audit.log_action("razorpay_webhook", payment_link_id or "unknown", "payment_confirmed", "unmatched",
                              details={"reason": "no_matching_session_for_payment_link",
                                       "payment_link_id": payment_link_id})

    return {"ok": True}
