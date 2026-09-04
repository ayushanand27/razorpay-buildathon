"""
Razorpay test-mode integration -- two separate rails:

  - create_payment_link() / create_order_with_retry(): Payment Links
    API, the HUMAN rail (POST /checkout) -- a human opens the returned
    URL to pay.
  - create_agent_order(): Orders API, the AGENT rail (POST /agent/pay)
    -- an AI agent has no browser to open a link in, so it gets a
    Razorpay Order object instead, which this demo then confirms via a
    signed simulate-capture call (see webhooks.py) rather than a real
    card payment.
  - create_refund(): Refunds API (POST /refund), either rail -- reverses
    a captured payment and restores stock (see orders.refund_order()).

Uses TEST mode keys only (rzp_test_...) -- get these free from the
Razorpay Dashboard -> Settings -> API Keys (test mode toggle on). No
real money ever moves.

The "one failure handled gracefully" demo path (simulate_failure=True
on /checkout, human rail only) is NOT a short-circuited fake raise. It
makes a genuinely invalid first request (amount=0), which Razorpay's
own API genuinely rejects with a real 400, then retries once with the
corrected amount. In mock mode (no keys configured) the same 400 JSON
shape is simulated locally, so this path is fully testable without
live credentials or network access.
"""

import os
import uuid
from pathlib import Path

import requests
from dotenv import load_dotenv
from requests.auth import HTTPBasicAuth

# FastAPI/uvicorn does not auto-load .env files -- load it explicitly.
# .env lives at the project root, one level above backend/.
load_dotenv(Path(__file__).resolve().parents[2] / ".env")

RAZORPAY_KEY_ID = os.environ.get("RAZORPAY_KEY_ID", "")
RAZORPAY_KEY_SECRET = os.environ.get("RAZORPAY_KEY_SECRET", "")
RAZORPAY_BASE = "https://api.razorpay.com/v1"


class PaymentFailure(Exception):
    def __init__(self, reason: str, retryable: bool, response_body: dict | None = None):
        self.reason = reason
        self.retryable = retryable
        self.response_body = response_body
        super().__init__(reason)


def _mock_400_body(reason: str) -> dict:
    """Same shape Razorpay's real API returns for a validation error --
    used only in mock mode, so the retry-and-recover path is testable
    without real keys or network access."""
    return {
        "error": {
            "code": "BAD_REQUEST_ERROR",
            "description": reason,
            "source": "business",
            "step": "payment_initiation",
            "reason": "input_validation_failed",
        }
    }


def create_payment_link(amount_inr: float, description: str, customer_contact: str = "9999999999"):
    """
    Creates a Razorpay test-mode Payment Link for a REAL, as-given
    amount -- this function does not special-case failure at all.
    Callers deliberately wanting the "invalid request" failure path
    (see create_order_with_retry) pass amount_inr=0 themselves.

    If RAZORPAY_KEY_ID / SECRET are not set, runs in MOCK mode so the
    rest of the system can be fully demoed without needing keys yet.
    """
    if not RAZORPAY_KEY_ID or not RAZORPAY_KEY_SECRET:
        if amount_inr <= 0:
            raise PaymentFailure("razorpay_api_error_400 (mock)", retryable=True,
                                  response_body=_mock_400_body("amount must be at least 100 paise"))
        return {
            "id": f"plink_MOCK{uuid.uuid4().hex[:10]}",
            "short_url": f"https://rzp.io/l/mock-{uuid.uuid4().hex[:8]}",
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
        try:
            body = resp.json()
        except ValueError:
            body = {"raw": resp.text}
        raise PaymentFailure(f"razorpay_api_error_{resp.status_code}", retryable=True, response_body=body)
    return resp.json()


def create_order_with_retry(amount_inr: float, description: str,
                              customer_contact: str = "9999999999",
                              simulate_failure: bool = False):
    """
    Returns (result, note, first_attempt_response, retry_attempt_response).

    When simulate_failure=True: makes a first, genuinely invalid call
    with amount_inr=0 -- Razorpay's real API rejects this with a real
    400 (or, with no keys configured, a realistically-shaped mock 400
    is raised locally) -- captures that response body, then retries
    once with the real, valid amount. `note` contains
    "recovered_after_retry" on success after the forced failure, or
    "failed_after_retry: ..." if even the corrected retry fails.
    """
    first_body = None
    if simulate_failure:
        try:
            create_payment_link(0, description, customer_contact)
        except PaymentFailure as e:
            first_body = e.response_body

    try:
        result = create_payment_link(amount_inr, description, customer_contact)
        note = "recovered_after_retry (first_attempt_amount_0)" if first_body else None
        return result, note, first_body, (result if first_body else None)
    except PaymentFailure as e:
        return None, f"failed_after_retry: {e.reason}", first_body, e.response_body


def create_agent_order(amount_inr: float, receipt: str, notes: dict | None = None) -> dict:
    """
    Creates a Razorpay test-mode Order (Orders API) -- the agent rail's
    equivalent of create_payment_link(). Returns a real order object if
    RAZORPAY_KEY_ID/SECRET are set, or a realistically-shaped mock one
    otherwise. Raises PaymentFailure (not retryable -- there is no
    "one failure handled gracefully" demo path on this rail) on a real
    API error.
    """
    if not RAZORPAY_KEY_ID or not RAZORPAY_KEY_SECRET:
        return {
            "id": f"order_MOCK{uuid.uuid4().hex[:10]}",
            "amount": int(amount_inr * 100),
            "currency": "INR",
            "receipt": receipt,
            "status": "created",
            "mock": True,
        }

    payload = {
        "amount": int(amount_inr * 100),  # paise
        "currency": "INR",
        "receipt": receipt,
        "notes": notes or {},
    }
    resp = requests.post(
        f"{RAZORPAY_BASE}/orders",
        json=payload,
        auth=HTTPBasicAuth(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET),
        timeout=10,
    )
    if resp.status_code >= 400:
        try:
            body = resp.json()
        except ValueError:
            body = {"raw": resp.text}
        raise PaymentFailure(f"razorpay_api_error_{resp.status_code}", retryable=False, response_body=body)
    return resp.json()


def create_refund(payment_id: str, amount_inr: float) -> dict:
    """
    Issues a Razorpay test-mode refund against a captured payment (real
    Razorpay Refunds API -- POST /v1/payments/{id}/refund -- or a mock
    response if no keys are configured). Raises PaymentFailure (not
    retryable) on a real API error. Full refunds only -- no partial-
    amount support, matching orders.refund_order()'s all-or-nothing
    stock restoration.
    """
    if not RAZORPAY_KEY_ID or not RAZORPAY_KEY_SECRET:
        return {
            "id": f"rfnd_MOCK{uuid.uuid4().hex[:10]}",
            "payment_id": payment_id,
            "amount": int(amount_inr * 100),
            "status": "processed",
            "mock": True,
        }

    resp = requests.post(
        f"{RAZORPAY_BASE}/payments/{payment_id}/refund",
        json={"amount": int(amount_inr * 100)},
        auth=HTTPBasicAuth(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET),
        timeout=10,
    )
    if resp.status_code >= 400:
        try:
            body = resp.json()
        except ValueError:
            body = {"raw": resp.text}
        raise PaymentFailure(f"razorpay_api_error_{resp.status_code}", retryable=False, response_body=body)
    return resp.json()
