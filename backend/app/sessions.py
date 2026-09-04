"""
Session store -- the server-side source of truth for "who is this
buyer". Replaces the old pattern of trusting a client-supplied `actor`
field in the request body (a client could simply claim
actor="human_whatsapp" and dodge every AI-agent guardrail). The actor
is now fixed at session-creation time and looked up from here on every
subsequent cart/checkout call -- it is never read from a request body
again.

Two ways to get a session:
  - POST /session/human -- no proof required; a human is already the
    accountable party, so there's nothing to authorize beyond "you are
    a person using the web chat".
  - POST /session/agent -- requires a spending warrant signed with
    AGENT_WARRANT_SECRET (HMAC-SHA256 over the warrant's canonical
    JSON). The warrant is what actually grants an AI agent the right
    to spend money on this merchant's behalf, and carries its own
    per-transaction / daily caps and allowed product categories --
    guardrails.py reads those from the session's warrant, not from a
    global constant, so a warrant-less agent session cannot exist and
    there is no permissive fallback.
"""

import hashlib
import hmac
import json
import os
import time
import uuid
from pathlib import Path

from dotenv import load_dotenv

# .env lives at the project root, one level above backend/.
load_dotenv(Path(__file__).resolve().parents[2] / ".env")

AGENT_WARRANT_SECRET = os.environ.get("AGENT_WARRANT_SECRET", "")
MERCHANT_ID = os.environ.get("MERCHANT_ID", "demo_merchant")

HUMAN_ACTOR = "human_whatsapp"
AGENT_ACTOR = "ai_agent_mcp"

REQUIRED_WARRANT_FIELDS = (
    "agent_id", "merchant_id", "per_tx_cap_inr", "daily_cap_inr",
    "allowed_categories", "expires_at", "nonce",
)

_SESSIONS: dict[str, dict] = {}

# Anti-replay pool for warrant issuance (POST /session/agent). Real
# Razorpay webhooks carry no nonce -- only a body signature -- so
# /demo/simulate-capture doesn't use this pool; a signed capture event
# is naturally idempotent anyway (orders.capture_order() no-ops on an
# already-captured order).
_USED_NONCES: set[str] = set()


class WarrantInvalid(Exception):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def canonical_json(obj: dict) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def sign_warrant(warrant: dict, secret: str | None = None) -> str:
    secret = AGENT_WARRANT_SECRET if secret is None else secret
    return hmac.new(secret.encode("utf-8"), canonical_json(warrant).encode("utf-8"), hashlib.sha256).hexdigest()


def consume_nonce(nonce: str | None) -> bool:
    """True (and marks it used) the first time a nonce is seen. False on
    a replay or a missing nonce."""
    if not nonce or nonce in _USED_NONCES:
        return False
    _USED_NONCES.add(nonce)
    return True


def _verify_warrant(warrant: dict, signature: str):
    if not AGENT_WARRANT_SECRET:
        raise WarrantInvalid("agent_warrant_secret_not_configured")
    missing = [f for f in REQUIRED_WARRANT_FIELDS if f not in warrant]
    if missing:
        raise WarrantInvalid(f"warrant_missing_fields: {','.join(missing)}")
    if not hmac.compare_digest(sign_warrant(warrant), signature or ""):
        raise WarrantInvalid("signature_mismatch")
    if warrant["merchant_id"] != MERCHANT_ID:
        raise WarrantInvalid("merchant_id_mismatch")
    if warrant["expires_at"] < time.time():
        raise WarrantInvalid("warrant_expired")
    if not consume_nonce(warrant["nonce"]):
        raise WarrantInvalid("nonce_reused_or_missing")


def create_human_session() -> str:
    session_id = f"human_{uuid.uuid4().hex[:12]}"
    _SESSIONS[session_id] = {"actor": HUMAN_ACTOR, "warrant": None, "created_at": time.time()}
    return session_id


def create_agent_session(warrant: dict, signature: str) -> str:
    """Raises WarrantInvalid on any failure -- caller maps that to a 401.
    The signature is stored alongside the warrant so policy.py can
    re-verify BOTH at payment time, not just at session-mint time --
    a warrant valid when the session was created may have since
    expired by the time the agent actually calls POST /agent/pay."""
    _verify_warrant(warrant, signature)
    session_id = f"agent_{uuid.uuid4().hex[:12]}"
    _SESSIONS[session_id] = {"actor": AGENT_ACTOR, "warrant": warrant,
                              "warrant_signature": signature, "created_at": time.time()}
    return session_id


def get_session(session_id: str) -> dict | None:
    return _SESSIONS.get(session_id)
