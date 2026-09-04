"""
Session store -- the server-side source of truth for "who is this
buyer, at which merchant". Replaces the old pattern of trusting a
client-supplied `actor` field in the request body. The actor AND
merchant are fixed at session-creation time and looked up from here on
every subsequent cart/checkout call -- they are never read from a
request body again.

Multi-tenant: every session belongs to exactly one merchant (a real
foreign key to db.Merchant, not a hardcoded registry -- see
merchant_registry.py). An agent session's warrant is verified against
THAT merchant's own warrant secret, not a single global one -- a
warrant signed for one merchant can never mint a session against a
different one, even if an attacker somehow learned another merchant's
id.

Backed by db.py's shared SQLModel database (the `sessions` table) --
part of the single app.db file, not a separate one -- so a backend
restart no longer silently logs out every buyer mid-session, and a
buyer session, its cart, and its orders can all be joined in one query
if ever needed.
"""

import hashlib
import hmac
import json
import os
import time
import uuid
from pathlib import Path

from dotenv import load_dotenv

from . import db, merchant_registry

# .env lives at the project root, one level above backend/.
load_dotenv(Path(__file__).resolve().parents[2] / ".env")

# Kept for backward compatibility with any caller still importing this
# directly -- merchant_registry.get_warrant_secret(merchant_id) is the
# actual source of truth used by _verify_warrant() below.
AGENT_WARRANT_SECRET = os.environ.get("AGENT_WARRANT_SECRET", "")
MERCHANT_ID = os.environ.get("MERCHANT_ID", "demo_merchant")

HUMAN_ACTOR = "human_whatsapp"
AGENT_ACTOR = "ai_agent_mcp"

REQUIRED_WARRANT_FIELDS = (
    "agent_id", "merchant_id", "per_tx_cap_inr", "daily_cap_inr",
    "allowed_categories", "expires_at", "nonce",
)


class WarrantInvalid(Exception):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def canonical_json(obj: dict) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def sign_warrant(warrant: dict, secret: str | None = None) -> str:
    """secret defaults to the warrant's own claimed merchant's secret
    for backward compatibility with existing callers/tests that don't
    pass one explicitly."""
    if secret is None:
        secret = merchant_registry.get_warrant_secret(warrant.get("merchant_id", "demo_merchant")) or AGENT_WARRANT_SECRET
    return hmac.new(secret.encode("utf-8"), canonical_json(warrant).encode("utf-8"), hashlib.sha256).hexdigest()


def consume_nonce(nonce: str | None) -> bool:
    """True (and marks it used) the first time a nonce is seen. False on
    a replay or a missing nonce. A real PRIMARY KEY INSERT is atomic at
    the database engine level -- two concurrent callers (even in
    different processes) racing the same nonce can't both "win". One
    pool shared across all merchants -- a nonce is a one-time-use
    random token regardless of which merchant it was issued for."""
    if not nonce:
        return False
    from sqlalchemy.exc import IntegrityError
    try:
        with db.get_session() as s:
            s.add(db.UsedNonce(nonce=nonce, created_at=time.time()))
        return True
    except IntegrityError:
        return False


def _verify_warrant(merchant_id: str, warrant: dict, signature: str):
    merchant = merchant_registry.get_merchant(merchant_id)
    if not merchant:
        raise WarrantInvalid("unknown_merchant")

    secret = merchant_registry.get_warrant_secret(merchant_id)
    if not secret:
        raise WarrantInvalid("agent_warrant_secret_not_configured")

    missing = [f for f in REQUIRED_WARRANT_FIELDS if f not in warrant]
    if missing:
        raise WarrantInvalid(f"warrant_missing_fields: {','.join(missing)}")
    if not hmac.compare_digest(sign_warrant(warrant, secret=secret), signature or ""):
        raise WarrantInvalid("signature_mismatch")
    if warrant["merchant_id"] != merchant_id:
        raise WarrantInvalid("merchant_id_mismatch")
    if warrant["expires_at"] < time.time():
        raise WarrantInvalid("warrant_expired")
    if not consume_nonce(warrant["nonce"]):
        raise WarrantInvalid("nonce_reused_or_missing")


def create_human_session(merchant_id: str) -> str:
    session_id = f"human_{uuid.uuid4().hex[:12]}"
    with db.get_session() as s:
        s.add(db.BuyerSession(session_id=session_id, merchant_id=merchant_id, actor=HUMAN_ACTOR))
    return session_id


def create_agent_session(merchant_id: str, warrant: dict, signature: str) -> str:
    """Raises WarrantInvalid on any failure -- caller maps that to a 401.
    The signature is stored alongside the warrant so policy.py can
    re-verify BOTH at payment time, not just at session-mint time --
    a warrant valid when the session was created may have since
    expired by the time the agent actually calls POST /agent/pay."""
    _verify_warrant(merchant_id, warrant, signature)
    session_id = f"agent_{uuid.uuid4().hex[:12]}"
    with db.get_session() as s:
        s.add(db.BuyerSession(
            session_id=session_id, merchant_id=merchant_id, actor=AGENT_ACTOR,
            warrant_json=json.dumps(warrant), warrant_signature=signature,
        ))
    return session_id


def get_session(session_id: str) -> dict | None:
    with db.get_session() as s:
        row = s.get(db.BuyerSession, session_id)
        if not row:
            return None
        return {
            "merchant_id": row.merchant_id,
            "actor": row.actor,
            "warrant": json.loads(row.warrant_json) if row.warrant_json else None,
            "warrant_signature": row.warrant_signature,
            "created_at": row.created_at,
        }


def set_warrant_for_tests(session_id: str, warrant: dict, signature: str):
    """Test-only helper -- overwrites an existing agent session's stored
    warrant/signature (e.g. to simulate a warrant that has since
    expired, or been re-signed to match a mutated warrant), the same
    way a real re-authorization would update it."""
    with db.get_session() as s:
        row = s.get(db.BuyerSession, session_id)
        if row:
            row.warrant_json = json.dumps(warrant)
            row.warrant_signature = signature
            s.add(row)
