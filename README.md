# Agentic Commerce for the Small Merchant
**Razorpay AI Buildathon 2026 — Track 1: AI Growth & Agentic Commerce**

## What this is

One merchant backend, **two front doors**:

1. **A human buyer**, chatting on a WhatsApp-style interface
2. **An AI buyer** (any MCP-compatible agent), completing a
   purchase autonomously through an MCP server

Both hit the **exact same** cart, checkout, guardrail and audit-trail
logic — there is no separate "toy" path for the AI demo. That's the
whole point: it proves this merchant is genuinely transactable by
both a person and an AI agent end to end, not just chat-assisted.

This is built to mirror the pattern Razorpay itself is already
piloting — Razorpay + NPCI's agentic-UPI pilot with Zomato,
Swiggy and Zepto (Feb 2026), and the direction NPCI's own Unified
Agent Protocol is heading (consent + a registered spending cap,
rather than per-transaction OTP).

## Why this architecture

The track's stated bar is:
> "Every money action explainable, bounded and gated. Show the audit
> trail and one failure handled gracefully."

Each word of that maps directly to a module:

| Requirement | Where it lives |
|---|---|
| Explainable | `backend/app/audit.py` — every action, from any actor, is appended to an audit log with actor, amount, status, and reason |
| Bounded | `backend/app/guardrails.py` — AI-agent purchases are capped at ₹2,000/transaction; out-of-stock items are blocked before checkout even starts |
| Gated | `mcp_server/server.py` — `checkout()` requires an explicit `confirm=True`, only after the agent has shown the buyer the cart total via `view_cart()` |
| One failure, handled gracefully | `backend/app/payments.py` — `create_payment_link_with_retry()`; trigger it via `simulate_failure=True` on the checkout call, watch it fail once, auto-retry, recover, and log both the failure and the recovery |

## Architecture

```
                     ┌─────────────────────────┐
  Human (WhatsApp)   │   backend/app (FastAPI)  │   AI agent
  web_chat/index.html│                          │   (via MCP)
        │            │  catalog.py  cart.py     │        │
        └───────────►│  guardrails.py           │◄───────┘
                      │  payments.py (Razorpay   │   mcp_server/server.py
                      │   test-mode Payment Links)│  (browse_catalog,
                      │  audit.py (SQLite)        │   add_to_cart,
                      └─────────────────────────┘   checkout, ...)
```

Single source of truth = the FastAPI backend. Nothing about checkout
logic, spending caps, or stock checks is duplicated between the two
front doors.

## Stack (100% free, no paid tools used to build this)

- Python + FastAPI — backend
- SQLite — audit trail (built into Python, no external DB needed)
- Official `mcp` Python SDK — MCP server
- Plain HTML/JS — human-buyer chat simulation (swap for the real
  WhatsApp Business/Twilio Sandbox API later — Twilio's WhatsApp
  Sandbox is free for dev/testing)
- Razorpay **test-mode** API keys — free, no real money moves
- Any MCP-compatible desktop client (free tier) — used as the MCP
  client to demo the AI-buyer flow live

## Running it locally

### 1. Backend
```bash
cd backend
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp ../.env.example ../.env   # fill in Razorpay TEST keys, or leave blank for mock mode
uvicorn app.main:app --port 8123
```

### 2. Human buyer demo
Just open `web_chat/index.html` in a browser (backend must be running).
Try: `catalog` → `add sku_001` → `cart` → `checkout`, and separately
`fail demo` to see the graceful-retry path.

### 3. AI buyer demo (MCP)
```bash
cd mcp_server
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
```
Then point your MCP client's config at `server.py` — see
`mcp_client_config.example.json` for the exact format. Restart
the client, and ask it to "browse the demo merchant's catalog and
buy me a water bottle" — it will call the tools live.

### 4. Audit trail
```
GET http://127.0.0.1:8123/audit-trail
```
Shows every action from both flows, interleaved, in order.

## Guardrails, explained

- AI-agent-initiated purchases are capped at **₹2,000 per
  transaction** (`AI_AGENT_SPENDING_CAP_INR` in `guardrails.py`) —
  loosely modeled on the consent + spending-limit pattern used in
  NPCI's proposed Unified Agent Protocol and Google's AP2, rather
  than trusting an agent with an unbounded payment API.
- Checkout always requires an explicit confirm step from the calling
  agent, after it has shown the buyer the total.
- Out-of-stock items are blocked before a payment link is even
  attempted.

## The one deliberately-handled failure

Pass `simulate_failure=true` to `/checkout` (or type `fail demo` in
the web chat, or ask the AI agent to call `checkout(confirm=True,
simulate_failure=True)`). The first payment-link attempt fails on
purpose; the system catches it, retries once with the corrected
request, succeeds, and logs **both** the failure and the recovery to
the audit trail — so the failure is visible, not hidden.

## What's mocked vs. real

- Razorpay Payment Links: real API call if `.env` has test keys, and
  clearly-labeled mock response if not — so the whole system is
  demoable with zero external accounts, and becomes fully live the
  moment test keys are added.
- WhatsApp: simulated via a web chat UI with the same message
  patterns a real WhatsApp Business API integration would use;
  swapping in Twilio's WhatsApp Sandbox is a drop-in replacement for
  `web_chat/`, not a backend change.

## Known limitations (honesty over polish)

- Single demo merchant, in-memory catalog — not multi-tenant yet.
- Spending cap and guardrail rules are static, not per-merchant
  configurable — the next real step here.
- No refund/dispute flow yet — checkout only.
