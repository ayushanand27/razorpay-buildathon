"""
Razorpay test-mode integration via the Payment Links API.

Uses TEST mode keys only (rzp_test_...) -- get these free from the
Razorpay Dashboard -> Settings -> API Keys (test mode toggle on).
No real money ever moves. This module also contains the ONE
deliberately-triggerable failure path used in the pitch video demo
(see simulate_failure=True) -- the buildathon bar explicitly wants
"one failure handled gracefully" shown, not just a happy path.
"""

import os
import requests
from requests.auth import HTTPBasicAuth

RAZORPAY_KEY_ID = os.environ.get("RAZORPAY_KEY_ID", "")
RAZORPAY_KEY_SECRET = os.environ.get("RAZORPAY_KEY_SECRET", "")
RAZORPAY_BASE = "https://api.razorpay.com/v1"


class PaymentFailure(Exception):
    def __init__(self, reason: str, retryable: bool):
        self.reason = reason
        self.retryable = retryable
        super().__init__(reason)


def create_payment_link(amount_inr: float, description: str,
                          customer_contact: str = "9999999999",
                          simulate_failure: bool = False):
    """
    Creates a Razorpay test-mode Payment Link.

    If RAZORPAY_KEY_ID / SECRET are not set, runs in MOCK mode so the
    rest of the system (audit trail, guardrails, MCP tools, WhatsApp
    flow) can be fully demoed without needing keys yet -- swap in
    real test keys any time via a .env file, nothing else changes.
    """
    if simulate_failure:
        # Deliberately triggered for the "one failure handled
        # gracefully" demo -- e.g. amount below Razorpay's minimum,
        # or an expired/invalid state.
        raise PaymentFailure("payment_link_creation_failed_amount_too_low", retryable=True)

    if not RAZORPAY_KEY_ID or not RAZORPAY_KEY_SECRET:
        # MOCK MODE -- no real API call, safe for local dev/demo.
        return {
            "id": "plink_MOCK123",
            "short_url": "https://rzp.io/l/mock-checkout-link",
            "status": "created",
            "amount": int(amount_inr * 100),
            "mock": True,
        }

    payload = {
        "amount": int(amount_inr * 100),  # paise
        "currency": "INR",
        "description": description,
        "customer": {"contact": customer_contact},
        "notify": {"sms": False, "email": False},
        "reminder_enable": False,
    }
    resp = requests.post(
        f"{RAZORPAY_BASE}/payment_links",
        json=payload,
        auth=HTTPBasicAuth(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET),
        timeout=10,
    )
    if resp.status_code >= 400:
        raise PaymentFailure(f"razorpay_api_error_{resp.status_code}: {resp.text}", retryable=True)
    return resp.json()


def create_payment_link_with_retry(amount_inr: float, description: str,
                                     customer_contact: str = "9999999999",
                                     simulate_failure: bool = False):
    """
    Wraps create_payment_link with ONE graceful retry using a slightly
    adjusted request -- this is the "handled gracefully" half of the
    demo, not just the failure itself.
    """
    try:
        return create_payment_link(amount_inr, description, customer_contact,
                                     simulate_failure=simulate_failure), None
    except PaymentFailure as e:
        if not e.retryable:
            return None, e.reason
        # Graceful recovery: retry once without the forced failure flag,
        # i.e. the real, corrected request.
        try:
            result = create_payment_link(amount_inr, description, customer_contact,
                                           simulate_failure=False)
            return result, f"recovered_after_retry ({e.reason})"
        except PaymentFailure as e2:
            return None, f"failed_after_retry: {e2.reason}"
