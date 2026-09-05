"""
MCP server for the demo merchant.

This is the "make a merchant transactable by an AI buyer end to end"
half of Track 1. Any MCP-aware client or agent can attach to this
server and autonomously browse the catalog and complete a purchase --
calling the exact same backend / policy engine / audit trail as the
WhatsApp human-buyer flow, not a separate toy path.

Sessions are per-buyer, not process-global: every tool that needs one
accepts an optional session_id argument. Omit it and the tool mints a
fresh agent session (signing a new warrant) on first use and reuses
that as this process's default from then on -- fine for a single
interactive conversation. A server fronting multiple concurrent buyers
should have each buyer's agent pass its own session_id explicitly
(returned by add_to_cart/pay/etc. on every call) instead of relying on
the default. Call start_new_session() to explicitly discard the
current default and mint a genuinely fresh one (empty cart, full daily
cap) rather than getting stuck with a cart only remove_from_cart can
fix.

Tools: browse_catalog, add_to_cart, remove_from_cart, start_new_session,
view_cart, remaining_cap, explain_last_block, pay, refund, get_metrics,
list_merchants, get_audit_trail. Resource: merchant://catalog.

The backend no longer trusts a bare actor="ai_agent_mcp" field in the
request body (see backend/app/sessions.py) -- this server proves it's
actually an authorized AI agent by presenting a spending warrant,
signed with AGENT_WARRANT_SECRET, to POST /session/agent. That secret
is shared with the merchant backend via the same root .env file -- in
this demo, the same operator runs both sides; in a real deployment the
warrant would instead be issued by the merchant ahead of time and
handed to the agent platform out of band.

Payment goes through POST /agent/pay (the Orders-API agent rail, split
from the human Payment-Links rail at /checkout) -- see
backend/app/policy.py for the 8-rule decision that runs on every call.

Run:
    BACKEND_URL=http://127.0.0.1:8123 python server.py

Then point your MCP client's config at this script (see README).
"""

import hashlib
import hmac
import json
import os
import time
import uuid
from pathlib import Path

import requests
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

# .env lives at the project root, one level above mcp_server/.
load_dotenv(Path(__file__).resolve().parents[1] / ".env")

BACKEND_URL = os.environ.get("BACKEND_URL", "http://127.0.0.1:8123")
AGENT_WARRANT_SECRET = os.environ.get("AGENT_WARRANT_SECRET", "")
MERCHANT_ID = os.environ.get("MERCHANT_ID", "demo_merchant")

# Caps this demo agent is authorized for -- must match the numbers
# DEMO_SCRIPT.md's scenarios assume.
AGENT_PER_TX_CAP_INR = 2000
AGENT_DAILY_CAP_INR = 5000
AGENT_ALLOWED_CATEGORIES = ["electronics", "apparel", "home", "stationery"]


def _sign_warrant(warrant: dict) -> str:
    canonical = json.dumps(warrant, sort_keys=True, separators=(",", ":"))
    return hmac.new(AGENT_WARRANT_SECRET.encode("utf-8"), canonical.encode("utf-8"), hashlib.sha256).hexdigest()


def _mint_agent_session() -> str:
    if not AGENT_WARRANT_SECRET:
        raise RuntimeError(
            "AGENT_WARRANT_SECRET is not set -- copy .env.example to .env at the "
            "project root and fill it in (see that file for details)."
        )
    warrant = {
        "agent_id": "mcp_demo_agent",
        "merchant_id": MERCHANT_ID,
        "per_tx_cap_inr": AGENT_PER_TX_CAP_INR,
        "daily_cap_inr": AGENT_DAILY_CAP_INR,
        "allowed_categories": AGENT_ALLOWED_CATEGORIES,
        "expires_at": time.time() + 3600,
        "nonce": uuid.uuid4().hex,
    }
    resp = requests.post(
        f"{BACKEND_URL}/session/agent",
        json={"warrant": warrant, "signature": _sign_warrant(warrant)},
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()["session_id"]


# Lazily-minted, process-default session -- used only when a tool call
# doesn't pass its own session_id (single-buyer convenience). Each
# concrete buyer conversation should track and pass its own
# session_id instead once it has one.
_default_session_id: str | None = None


def _resolve_session(session_id: str | None) -> str:
    global _default_session_id
    if session_id:
        return session_id
    if not _default_session_id:
        _default_session_id = _mint_agent_session()
    return _default_session_id


mcp = FastMCP("razorpay-demo-merchant")


@mcp.resource("merchant://catalog", mime_type="application/json")
def catalog_resource() -> dict:
    """The merchant's full product catalog -- same agent-readable data
    as GET /catalog (sku, name, description, price_inr, currency,
    tax_bps, stock, availability, category, attributes, return_window_days)."""
    resp = requests.get(f"{BACKEND_URL}/catalog", timeout=10)
    resp.raise_for_status()
    return resp.json()


@mcp.tool()
def browse_catalog(category: str | None = None) -> dict:
    """Browse the merchant's product catalog. Optionally filter by category
    (electronics, apparel, home, stationery)."""
    params = {"category": category} if category else {}
    resp = requests.get(f"{BACKEND_URL}/catalog", params=params, timeout=10)
    resp.raise_for_status()
    return resp.json()


@mcp.tool()
def add_to_cart(product_id: str, qty: int = 1, session_id: str | None = None) -> dict:
    """Add a product to the buyer's cart by its product_id (e.g. 'sku_001').
    Pass session_id to act on a specific buyer's cart; omit it to use
    (and, on first call, mint) this process's default session. The
    response always includes session_id -- remember it for this buyer's
    later calls."""
    sid = _resolve_session(session_id)
    resp = requests.post(
        f"{BACKEND_URL}/cart/add",
        json={"session_id": sid, "product_id": product_id, "qty": qty},
        timeout=10,
    )
    if resp.status_code >= 400:
        return {"error": resp.json().get("detail", "add_to_cart_failed"), "session_id": sid}
    return {**resp.json(), "session_id": sid}


@mcp.tool()
def remove_from_cart(product_id: str, session_id: str | None = None) -> dict:
    """Remove a product entirely from the buyer's cart by its product_id
    (e.g. 'sku_001') -- removes the whole line item, not a partial
    quantity decrement. Use this to fix a cart before paying (e.g. the
    buyer changed their mind, or a previous add_to_cart call ended up
    with the wrong quantity) instead of trying to add a negative
    quantity, which isn't a thing. Like any cart mutation, this
    invalidates a prior view_cart() review -- call view_cart() again
    before pay()."""
    sid = _resolve_session(session_id)
    resp = requests.post(
        f"{BACKEND_URL}/cart/remove",
        json={"session_id": sid, "product_id": product_id},
        timeout=10,
    )
    if resp.status_code >= 400:
        return {"error": resp.json().get("detail", "remove_from_cart_failed"), "session_id": sid}
    return {**resp.json(), "session_id": sid}


@mcp.tool()
def start_new_session() -> dict:
    """Mint a brand new agent session (a fresh signed warrant, empty
    cart, zero spend against the daily cap so far this call) and make
    it this process's default going forward -- use this when you want
    to start over cleanly, e.g. the current session's cart got into a
    state you can't fix with remove_from_cart/add_to_cart alone.
    Unlike omitting session_id on another tool (which reuses whatever
    default session already exists), this ALWAYS creates a genuinely
    new one. The returned session_id is also usable explicitly on any
    other tool call if you're juggling more than one buyer."""
    global _default_session_id
    _default_session_id = _mint_agent_session()
    return {"session_id": _default_session_id, "note": "fresh session, empty cart, full daily cap remaining"}


@mcp.tool()
def view_cart(session_id: str | None = None) -> dict:
    """View a buyer's cart contents and running total. This ALSO marks
    the cart as reviewed, which pay() requires -- call this immediately
    before pay(), not just once at the start."""
    sid = _resolve_session(session_id)
    resp = requests.get(f"{BACKEND_URL}/cart/{sid}", timeout=10)
    resp.raise_for_status()
    return {**resp.json(), "session_id": sid}


@mcp.tool()
def remaining_cap(session_id: str | None = None) -> dict:
    """Check this buyer's authorized spending room: per-transaction cap,
    how much of the daily cap remains (captured spend only), and when
    the underlying warrant expires."""
    sid = _resolve_session(session_id)
    resp = requests.get(f"{BACKEND_URL}/agent/remaining-cap", params={"session_id": sid}, timeout=10)
    resp.raise_for_status()
    return {**resp.json(), "session_id": sid}


@mcp.tool()
def explain_last_block(session_id: str | None = None) -> dict:
    """Explain why this buyer's last pay() attempt was blocked, if any --
    returns the full policy decision (reason, remaining_cap_inr) logged
    at the time. Use this after a pay() call returns an error to
    understand exactly which rule blocked it."""
    sid = _resolve_session(session_id)
    resp = requests.get(f"{BACKEND_URL}/agent/explain-last-block", params={"session_id": sid}, timeout=10)
    resp.raise_for_status()
    return {**resp.json(), "session_id": sid}


@mcp.tool()
def pay(confirm: bool, idempotency_key: str, session_id: str | None = None) -> dict:
    """Pay for the buyer's current cart via the agent checkout rail
    (Razorpay Orders API). MUST be called with confirm=True only after
    the buyer has explicitly agreed to the cart total shown by
    view_cart() -- call view_cart() again right before this if any time
    has passed, since the review requirement resets on every cart
    mutation. idempotency_key is REQUIRED and must be a fresh, unique
    value per distinct purchase attempt -- reusing the same key for a
    retried call returns the original result instead of paying twice.
    Purchases are checked against this buyer's warrant (per-transaction
    and daily caps, allowed categories) and current stock; call
    explain_last_block() if this returns an error to see exactly why."""
    sid = _resolve_session(session_id)
    resp = requests.post(
        f"{BACKEND_URL}/agent/pay",
        json={"session_id": sid, "idempotency_key": idempotency_key, "confirm": confirm},
        timeout=15,
    )
    if resp.status_code >= 400:
        return {"error": resp.json().get("detail", "pay_failed"), "status_code": resp.status_code, "session_id": sid}
    return {**resp.json(), "session_id": sid}


@mcp.tool()
def refund(order_id: str, session_id: str | None = None) -> dict:
    """Refund a previously CAPTURED order -- reverses a real Razorpay
    payment (test mode) and restores stock. A session may only refund
    its OWN orders (the backend enforces this -- passing someone else's
    order_id fails, it doesn't silently succeed). Refunding an
    already-refunded order is a no-op that still reports success.
    Use this if the buyer changes their mind after a purchase already
    went through, not to cancel something still pending."""
    sid = _resolve_session(session_id)
    resp = requests.post(
        f"{BACKEND_URL}/refund",
        json={"session_id": sid, "order_id": order_id},
        timeout=15,
    )
    if resp.status_code >= 400:
        return {"error": resp.json().get("detail", "refund_failed"), "status_code": resp.status_code, "session_id": sid}
    return {**resp.json(), "session_id": sid}


@mcp.tool()
def get_metrics() -> dict:
    """This merchant's live growth numbers -- captured revenue,
    conversion rate, and upsell acceptance, computed straight from the
    audit trail. Read-only, no session needed -- same aggregated data
    web_chat/metrics.html shows, scoped to this MCP server's own
    merchant only (never another tenant's)."""
    resp = requests.get(f"{BACKEND_URL}/merchants/{MERCHANT_ID}/metrics", timeout=10)
    resp.raise_for_status()
    return resp.json()


@mcp.tool()
def list_merchants() -> dict:
    """List every merchant registered on this backend (id and name only
    -- no catalog, no secrets). Read-only, no session needed. Useful to
    confirm which merchant_id this server is actually configured
    against, or to see what else exists on a shared backend."""
    resp = requests.get(f"{BACKEND_URL}/merchants", timeout=10)
    resp.raise_for_status()
    return resp.json()


@mcp.tool()
def get_audit_trail(limit: int = 10, session_id: str | None = None) -> dict:
    """View recent audit log entries -- every action this buyer's own
    merchant has logged (human or AI actor), for transparency. The
    backend strictly scopes this to the caller's own session's
    merchant -- there is no unauthenticated or cross-merchant view.
    There is no un-prefixed /audit-trail route anymore either -- every
    caller names the merchant_id it's reading explicitly, matched
    against its own session."""
    sid = _resolve_session(session_id)
    resp = requests.get(f"{BACKEND_URL}/merchants/{MERCHANT_ID}/audit-trail",
                         params={"limit": limit, "session_id": sid}, timeout=10)
    resp.raise_for_status()
    return {**resp.json(), "session_id": sid}


if __name__ == "__main__":
    mcp.run(transport="stdio")
