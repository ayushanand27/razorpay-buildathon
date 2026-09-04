"""
Session store -- the server-side source of truth for "who is this
buyer, at which merchant". Replaces the old pattern of trusting a
client-supplied `actor` field in the request body (a client could
simply claim actor="human_whatsapp" and dodge every AI-agent
guardrail). The actor AND merchant are fixed at session-creation time
and looked up from here on every subsequent cart/checkout call -- they
are never read from a request body again.

Multi-tenant: every session belongs to exactly one merchant
(merchants.py). An agent session's warrant is verified against THAT
merchant's own warrant secret (merchants.get_warrant_secret), not a
single global one -- a warrant signed for one merchant can never mint
a session against a different one, even if an attacker somehow learned
another merchant's id.

Persisted in SQLite (sessions.db, alongside audit_trail.db) rather than
an in-memory dict -- a backend restart (a redeploy, a crash) no longer
silently logs out every buyer mid-session. Each mutating call opens
its own connection and commits immediately (same pattern as
audit.py), with a busy_timeout so concurrent requests wait for the
SQLite writer lock briefly instead of raising immediately.

Two ways to get a session:
  - POST /merchants/{merchant_id}/session/human -- no proof required;
    a human is already the accountable party, so there's nothing to
    authorize beyond "you are a person using the web chat".
  - POST /merchants/{merchant_id}/session/agent -- requires a spending
    warrant signed with that merchant's own warrant secret
    (HMAC-SHA256 over the warrant's canonical JSON). The warrant is
    what actually grants an AI agent the right to spend money on this
    merchant's behalf, and carries its own per-transaction / daily
    caps and allowed product categories -- policy.py reads those from
    the session's warrant, not from a global constant, so a
    warrant-less agent session cannot exist and there is no permissive
    fallback.
"""

import hashlib
import hmac
import json
import os
import sqlite3
import time
import uuid
from pathlib import Path

from dotenv import load_dotenv

from . import merchants

# .env lives at the project root, one level above backend/.
load_dotenv(Path(__file__).resolve().parents[2] / ".env")

# Kept for backward compatibility with any caller still importing this
# directly (e.g. a script signing a demo_merchant warrant by hand) --
# merchants.get_warrant_secret(merchant_id) is the actual source of
# truth used by _verify_warrant() below.
AGENT_WARRANT_SECRET = os.environ.get("AGENT_WARRANT_SECRET", "")
MERCHANT_ID = os.environ.get("MERCHANT_ID", "demo_merchant")

HUMAN_ACTOR = "human_whatsapp"
AGENT_ACTOR = "ai_agent_mcp"

REQUIRED_WARRANT_FIELDS = (
    "agent_id", "merchant_id", "per_tx_cap_inr", "daily_cap_inr",
    "allowed_categories", "expires_at", "nonce",
)

DB_PATH = os.path.join(os.path.dirname(__file__), "sessions.db")


def _get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sessions (
            session_id TEXT PRIMARY KEY,
            merchant_id TEXT NOT NULL,
            actor TEXT NOT NULL,
            warrant TEXT,             -- JSON, NULL for human sessions
            warrant_signature TEXT,   -- NULL for human sessions
            created_at REAL NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS used_nonces (
            nonce TEXT PRIMARY KEY,
            created_at REAL NOT NULL
        )
        """
    )
    return conn


class WarrantInvalid(Exception):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def canonical_json(obj: dict) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def sign_warrant(warrant: dict, secret: str | None = None) -> str:
    """secret defaults to the demo_merchant secret for backward
    compatibility with existing callers/tests that don't pass one
    explicitly; multi-merchant callers should pass
    merchants.get_warrant_secret(merchant_id) explicitly."""
    if secret is None:
        secret = merchants.get_warrant_secret(warrant.get("merchant_id", "demo_merchant")) or AGENT_WARRANT_SECRET
    return hmac.new(secret.encode("utf-8"), canonical_json(warrant).encode("utf-8"), hashlib.sha256).hexdigest()


def consume_nonce(nonce: str | None) -> bool:
    """True (and marks it used) the first time a nonce is seen. False on
    a replay or a missing nonce. A single INSERT OR IGNORE is atomic at
    the SQLite engine level -- two concurrent callers racing the same
    nonce can't both "win". One pool shared across all merchants -- a
    nonce is a one-time-use random token regardless of which merchant
    it was issued for, so there's no need to partition it."""
    if not nonce:
        return False
    conn = _get_conn()
    try:
        cur = conn.execute("INSERT OR IGNORE INTO used_nonces (nonce, created_at) VALUES (?, ?)",
                            (nonce, time.time()))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def _verify_warrant(merchant_id: str, warrant: dict, signature: str):
    merchant = merchants.get_merchant(merchant_id)
    if not merchant:
        raise WarrantInvalid("unknown_merchant")

    secret = merchants.get_warrant_secret(merchant_id)
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
    conn = _get_conn()
    try:
        conn.execute(
            "INSERT INTO sessions (session_id, merchant_id, actor, warrant, warrant_signature, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (session_id, merchant_id, HUMAN_ACTOR, None, None, time.time()),
        )
        conn.commit()
    finally:
        conn.close()
    return session_id


def create_agent_session(merchant_id: str, warrant: dict, signature: str) -> str:
    """Raises WarrantInvalid on any failure -- caller maps that to a 401.
    The signature is stored alongside the warrant so policy.py can
    re-verify BOTH at payment time, not just at session-mint time --
    a warrant valid when the session was created may have since
    expired by the time the agent actually calls POST /agent/pay."""
    _verify_warrant(merchant_id, warrant, signature)
    session_id = f"agent_{uuid.uuid4().hex[:12]}"
    conn = _get_conn()
    try:
        conn.execute(
            "INSERT INTO sessions (session_id, merchant_id, actor, warrant, warrant_signature, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (session_id, merchant_id, AGENT_ACTOR, json.dumps(warrant), signature, time.time()),
        )
        conn.commit()
    finally:
        conn.close()
    return session_id


def get_session(session_id: str) -> dict | None:
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT merchant_id, actor, warrant, warrant_signature, created_at FROM sessions WHERE session_id = ?",
            (session_id,),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    merchant_id, actor, warrant_json, warrant_signature, created_at = row
    return {
        "merchant_id": merchant_id,
        "actor": actor,
        "warrant": json.loads(warrant_json) if warrant_json else None,
        "warrant_signature": warrant_signature,
        "created_at": created_at,
    }


def set_warrant_for_tests(session_id: str, warrant: dict, signature: str):
    """Test-only helper -- overwrites an existing agent session's stored
    warrant/signature (e.g. to simulate a warrant that has since
    expired, or been re-signed to match a mutated warrant), the same
    way a real re-authorization would update it."""
    conn = _get_conn()
    try:
        conn.execute("UPDATE sessions SET warrant = ?, warrant_signature = ? WHERE session_id = ?",
                      (json.dumps(warrant), signature, session_id))
        conn.commit()
    finally:
        conn.close()
