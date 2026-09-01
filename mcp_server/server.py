"""
MCP server for the demo merchant.

This is the "make a merchant transactable by an AI buyer end to end"
half of Track 1. Any MCP-aware client or agent can attach to this
server and autonomously browse the catalog and complete a purchase
-- calling the exact same backend / guardrails / audit trail as the
WhatsApp human-buyer flow, not a separate toy path.

Run:
    BACKEND_URL=http://127.0.0.1:8123 python server.py

Then point your MCP client's config at this script (see README).
"""

import os
import uuid
import requests
from mcp.server.fastmcp import FastMCP

BACKEND_URL = os.environ.get("BACKEND_URL", "http://127.0.0.1:8123")
ACTOR = "ai_agent_mcp"

# One session id per server process for this demo -- in production
# this would be tied to the calling agent's authenticated identity.
SESSION_ID = f"mcp_{uuid.uuid4().hex[:8]}"

mcp = FastMCP("razorpay-demo-merchant")


@mcp.tool()
def browse_catalog(category: str | None = None) -> dict:
    """Browse the merchant's product catalog. Optionally filter by category
    (electronics, apparel, home, stationery)."""
    params = {"category": category} if category else {}
    resp = requests.get(f"{BACKEND_URL}/catalog", params=params, timeout=10)
    resp.raise_for_status()
    return resp.json()


@mcp.tool()
def add_to_cart(product_id: str, qty: int = 1) -> dict:
    """Add a product to the current cart by its product_id (e.g. 'sku_001')."""
    resp = requests.post(
        f"{BACKEND_URL}/cart/add",
        json={"session_id": SESSION_ID, "actor": ACTOR, "product_id": product_id, "qty": qty},
        timeout=10,
    )
    if resp.status_code >= 400:
        return {"error": resp.json().get("detail", "add_to_cart_failed")}
    return resp.json()


@mcp.tool()
def view_cart() -> dict:
    """View the current cart contents and running total."""
    resp = requests.get(f"{BACKEND_URL}/cart/{SESSION_ID}", timeout=10)
    resp.raise_for_status()
    return resp.json()


@mcp.tool()
def checkout(confirm: bool, simulate_failure: bool = False) -> dict:
    """Complete checkout for the current cart and get a Razorpay test-mode
    payment link. MUST be called with confirm=True only after the buyer
    (human or the calling AI on the human's behalf) has explicitly agreed
    to the cart total shown by view_cart -- this is the gate. Purchases
    are capped and will be blocked automatically if they exceed the
    AI-agent spending limit or if an item is out of stock.
    Set simulate_failure=True only for demo purposes to show the
    graceful-failure-and-retry path."""
    if not confirm:
        return {"error": "checkout_requires_explicit_confirm=True after showing the buyer the cart total"}

    resp = requests.post(
        f"{BACKEND_URL}/checkout",
        json={"session_id": SESSION_ID, "actor": ACTOR, "simulate_failure": simulate_failure},
        timeout=15,
    )
    if resp.status_code >= 400:
        return {"error": resp.json().get("detail", "checkout_failed"), "status_code": resp.status_code}
    return resp.json()


@mcp.tool()
def get_audit_trail(limit: int = 10) -> dict:
    """View recent audit log entries -- every action any actor (human or
    AI) has taken, for transparency."""
    resp = requests.get(f"{BACKEND_URL}/audit-trail", params={"limit": limit}, timeout=10)
    resp.raise_for_status()
    return resp.json()


if __name__ == "__main__":
    mcp.run(transport="stdio")
