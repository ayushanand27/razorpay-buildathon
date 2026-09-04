# Agentic Commerce for the Small Merchant
**Razorpay AI Buildathon 2026 — Track 1: AI Growth & Agentic Commerce**

## What this is

One merchant backend, **two front doors**:

1. **A human buyer**, chatting on a WhatsApp-style interface
2. **An AI buyer** (any MCP-compatible agent), completing a
   purchase autonomously through an MCP server

Both hit the **exact same** cart, catalog, and audit-trail logic —
there is no separate "toy" path for the AI demo. Payment itself is
split into two rails on purpose (a human has a browser to pay in; an
AI agent doesn't): the human pays via a Razorpay Payment Link
(`POST /checkout`), the AI agent pays via the Razorpay Orders API,
evaluated by its own deterministic policy engine and self-completed
immediately (`POST /agent/pay`) — see `backend/app/policy.py`. That's
the point either way: it proves this merchant is genuinely
transactable by both a person and an AI agent end to end, not just
chat-assisted.

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
| Explainable | `backend/app/audit.py` — every action, from any actor, is appended to an audit log with actor, amount, status, and reason. Every AGENT-rail payment decision (allow AND block) is additionally logged in full by `backend/app/policy.py` as a `policy_decision` entry — the exact JSON an agent can retrieve itself via `explain_last_block()`. Payment confirmation is tracked separately from order/link creation: `backend/app/webhooks.py` verifies an HMAC signature over the raw body (real Razorpay webhook OR the local `/demo/simulate-capture` stand-in — same verification function, same secret) and logs the confirmed payment as its own `payment_confirmed` entry |
| Bounded | `backend/app/policy.py` — AI-agent purchases are capped per-transaction AND cumulatively per day, every SKU must be in the warrant's allowed categories, and the server-computed total must match what the cart's own line items claim (a price-tamper check) — all read from a signed spending warrant re-verified on every single attempt, not a client-claimed value or a one-time check at session creation. Stock is checked per line item on both rails, and only ever decremented at payment-capture time, never at order-creation time (`backend/app/orders.py`, `backend/app/catalog.py`) |
| Gated | `backend/app/main.py` — both `/checkout` and `/agent/pay` require a valid session established via `POST /session/human` or `POST /session/agent` (the actor is never taken from the request body, so it can't be spoofed), AND a prior `GET /cart/{session_id}` review that's invalidated by any later cart mutation, not just checked once |
| One failure, handled gracefully | `backend/app/payments.py` — `create_order_with_retry()` (human rail only); trigger it via `simulate_failure=True` on the checkout call. The first attempt is a genuinely invalid request (amount=0) that Razorpay's real API genuinely rejects with a real 400; the system catches it, retries with the corrected amount, and logs both response bodies to the audit trail |
| AI Growth (quantified) | `backend/app/metrics.py` + `web_chat/metrics.html` — captured revenue (payment_confirmed only, not merely order/link-created, across BOTH rails), conversion rate, and upsell acceptance computed live from the audit trail |

## Architecture

```
                     ┌─────────────────────────────┐
  Human (WhatsApp)   │     backend/app (FastAPI)    │   AI agent
  web_chat/index.html│                              │   (via MCP)
        │  session   │ sessions.py   catalog.py     │  signed warrant
        │  (no       │ cart.py       orders.py      │  + session
        │  warrant)  │                              │        │
        └───────────►│ POST /checkout   POST /agent/pay      │
                      │  guardrails.py     policy.py │◄───────┘
                      │  (review + stock)  (8 rules, │   mcp_server/server.py
                      │       │                │     │  (browse_catalog,
                      │  Payment Links    Orders API │   add_to_cart, pay,
                      │  (payments.py)   (payments.py)│  remaining_cap,
                      │       │                │     │   explain_last_block)
                      │       └──── webhooks.py ─────┤
                      │        (real webhook OR       │
                      │      /demo/simulate-capture,   │
                      │       same verification)       │
                      │        audit.py (SQLite)       │
                      └─────────────────────────────┘
```

Single source of truth = the FastAPI backend. Catalog, cart, and audit
logic are never duplicated between the two front doors; payment is
deliberately split into two rails (human vs. agent) with their own
guard modules, since a browser-based Payment Link and an
agent-appropriate Orders-API-plus-policy-check are genuinely different
shapes of "pay for this."

## Stack (100% free, no paid tools used to build this)

- Python + FastAPI — backend
- SQLite — audit trail (built into Python, no external DB needed)
- Official `mcp` Python SDK — MCP server
- Plain HTML/JS — human-buyer chat simulation (swap for the real
  WhatsApp Business/Twilio Sandbox API later — Twilio's WhatsApp
  Sandbox is free for dev/testing)
- Razorpay **test-mode** API keys — free, no real money moves
- Any MCP-compatible desktop client (a free-tier one works fine) —
  used as the MCP client to demo the AI-buyer flow live

## Running it locally

### 1. Backend
```bash
cd backend
python3 -m venv venv && source venv/bin/activate   # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp ../.env.example ../.env   # fill in Razorpay TEST keys, or leave blank for mock mode
uvicorn app.main:app --port 8123
```
`.env` also carries `AGENT_WARRANT_SECRET` and `RAZORPAY_WEBHOOK_SECRET`,
which — unlike the Razorpay API keys — are required, not optional:
they're what an AI-agent session is authorized against, and what a
payment capture (real or simulated) is verified against, respectively
(see "Guardrails, explained" below). Working demo defaults ship in
`.env.example`; generate your own with `python -c "import secrets;
print(secrets.token_hex(24))"` for anything beyond a local demo.

Run the test suite (entirely in mock mode, no keys or network needed):
```bash
pytest
```

### 2. Human buyer demo
Just open `web_chat/index.html` in a browser (backend must be running).
Try: `catalog` → `add sku_001` → `cart` → `checkout`, and separately
`fail demo` to see the graceful-retry path. Two more pages sit alongside
it (same nav strip to jump between all three): `web_chat/catalog.html`
is a live product grid, and `web_chat/audit-dashboard.html` is a
readable, auto-refreshing view of the audit trail.

Anything typed that isn't one of those exact commands goes to
`POST /nlu/turn` (`backend/app/nlu.py`) — Groq-powered intent
classification restricted to a fixed tool set (`browse`, `add`,
`remove`, `view_cart`, `checkout`, `clarify`); it is never trusted for
a price or an allow/block decision, and every tool it picks is
executed through the exact same cart/checkout functions the hardcoded
commands call, so every guardrail still applies. Try: *"got anything
for the gym under 400"*, *"add the bottle and the earbuds"*, *"that's
too much, remove the earbuds"*, *"pay"*. A handful of unambiguous
phrasings (`pay`, `cart`, `catalog`, …) are matched locally before ever
calling Groq, to conserve its free-tier daily quota (also spent by the
upsell copy, see below) for text that actually needs it.

### 3. AI buyer demo (MCP)
```bash
cd mcp_server
python3 -m venv venv && source venv/bin/activate   # Windows: venv\Scripts\activate
pip install -r requirements.txt
python generate_mcp_config.py   # writes mcp_client_config.json with this machine's absolute paths -- no hand-editing
```
Then point your MCP client's config at the `mcp_client_config.json`
this writes (see `mcp_client_config.example.json` for the raw format
if you'd rather wire it up by hand). Restart the client, and ask it to
"browse the demo merchant's catalog and buy me a water bottle" — it
will call the tools live: `browse_catalog`, `add_to_cart`, `view_cart`,
`pay`, plus `remaining_cap` and `explain_last_block` for the agent to
check its own spending room or understand a block. Sessions are
per-buyer (each tool call accepts an optional `session_id`, minting one
on first use if omitted), not fixed once at server startup. The full
catalog is also exposed as an MCP resource, `merchant://catalog`.

### 4. Audit trail
```
GET http://127.0.0.1:8123/audit-trail
```
Shows every action from both flows, interleaved, in order.

## Guardrails, explained

- **Actor is never trusted from the request body.** `POST
  /session/human` and `POST /session/agent` are the only ways to get a
  `session_id`; every cart/checkout/pay call after that looks the
  actor up server-side from that session (`backend/app/sessions.py`).
  Posting `actor: "human_whatsapp"` from an AI-agent session is
  silently ignored, and `/checkout` rejects an agent session (and
  `/agent/pay` rejects a human one) outright — the two rails are
  enforced server-side, not a client-side convention.
- **AI-agent sessions require a signed spending warrant** —
  HMAC-SHA256 over the warrant's canonical JSON, keyed by
  `AGENT_WARRANT_SECRET`. The warrant carries its own
  `per_tx_cap_inr`, `daily_cap_inr`, and `allowed_categories`, an
  `expires_at`, and a one-time `nonce` — loosely modeled on the
  consent + spending-limit pattern used in NPCI's proposed Unified
  Agent Protocol and Google's AP2, rather than trusting an agent with
  an unbounded payment API. A warrant-less agent session cannot exist,
  and `backend/app/policy.py` re-verifies the SAME signature and
  expiry on every single `/agent/pay` call, not just once at session
  creation — a warrant that was valid when the session was minted but
  has since expired is caught at payment time too.
- **`policy.py`'s 8 rules, all of which must pass, in order:**
  warrant signature valid and not expired; merchant_id matches; every
  SKU in the warrant's allowed categories; server price × qty matches
  what the cart's own line items claim (a price-tamper check — the
  actual charge always uses the live server catalog price, never a
  client-influenced one); per-transaction cap; daily cap; cart
  reviewed since the last mutation; no line item over stock. It's a
  pure function of its inputs — same inputs always yield the same
  decision — and calls no LLM, directly or indirectly.
- **The daily cap counts CAPTURED spend PLUS any order still sitting
  "created"-but-uncaptured from today** (`orders.pending_spend_today`)
  — not just what's been captured. In today's architecture
  `/agent/pay` always self-captures synchronously in the same request,
  so this second half mainly guards against a rarer case: the process
  crashing between order-creation and self-capture, which would
  otherwise leave a "phantom" order that never counts against the cap.
- **The upsell suggestion is policy-bounded too, not just a slogan**
  (`catalog.get_upsell`) — it never suggests a SKU that would push the
  cart total past the buyer's remaining cap (agent: warrant's
  per-tx/daily remaining; human: `guardrails.MAX_ORDER_INR`), is
  out of stock, or is already in the cart. Every outcome is logged
  (`upsell_shown` / `upsell_blocked`, with a reason), and
  `GET /metrics` reports `upsell_blocked_by_cap_count` separately.
- **Every policy decision is logged in full**, allow or block, as a
  `policy_decision` audit entry — `POST /agent/pay`'s response and the
  MCP `explain_last_block()` tool both surface this.
- Checkout/pay is **idempotent** on `(session_id, idempotency_key)` on
  both rails — a retried/duplicated call returns the original order
  instead of creating a second one.
- **Stock is a per-line-item invariant**, checked at checkout/pay time
  and only ever decremented at payment-capture time — never at
  order-creation time. The agent rail self-captures synchronously
  inside `/agent/pay`; the human rail's capture happens later, via a
  real Razorpay webhook or the local `/demo/simulate-capture` stand-in.
  If a capture fails partway through a multi-item order, whatever was
  already decremented in that attempt is rolled back.
- A prior `GET /cart/{session_id}` review is required on both rails,
  and is invalidated by ANY later cart mutation (not just checked
  once) — enforced server-side, not just something an MCP tool's
  docstring asks nicely for.
- Out-of-stock items (or a requested qty exceeding what's in stock)
  are blocked before any payment is even attempted.

## The one deliberately-handled failure

Pass `simulate_failure=true` to `/checkout` (human rail only — or type
`fail demo` in the web chat). The first attempt is a genuinely invalid
request (amount=0) that Razorpay's real API genuinely rejects with a
real 400 (or, in mock mode, a realistically-shaped fake 400 — no
special-cased fake failure path exists anymore). The system catches
it, retries once with the corrected request, succeeds, and logs
**both** response bodies to the audit trail — so the failure is
visible, not hidden.

## What's mocked vs. real

- Razorpay Payment Links / Orders: real API call if `.env` has test
  keys, and clearly-labeled mock response if not — so the whole system
  is demoable with zero external accounts, and becomes fully live the
  moment test keys are added.
- Payment **capture** is always simulated in this demo, on both rails
  — there's no public URL for Razorpay to call locally, and an AI
  agent has no browser to complete a real card payment in anyway. The
  simulate-capture path (`backend/app/webhooks.py`) uses the exact
  same signature verification as a real Razorpay webhook would, so
  swapping in a real one (pointed at `POST /webhook/razorpay`) is a
  configuration change, not a code change.
- WhatsApp: simulated via a web chat UI with the same message
  patterns a real WhatsApp Business API integration would use;
  swapping in Twilio's WhatsApp Sandbox is a drop-in replacement for
  `web_chat/`, not a backend change.

## Known limitations (honesty over polish)

- Single demo merchant, in-memory catalog — not multi-tenant yet.
- Sessions, orders, and carts are all in-memory — they reset with the
  process, same as the catalog. A real deployment would persist these.
- No refund/dispute flow yet — checkout only.
- The MCP server's per-buyer sessions are process-local, tracked by
  whichever agent conversation calls the tools; there's no persistent
  buyer-identity store across separate MCP server restarts.
- `POST /webhook/razorpay` has never actually been called by a real
  Razorpay deployment — there's no public URL for it to reach in this
  local setup, so it's only ever been exercised by the local
  `/demo/simulate-capture` stand-in (same verification code, but not
  proof the integration survives Razorpay's actual delivery behavior).
- Groq's free tier (1,000 requests/day, shared across upsell-copy
  generation AND chat NLU) is easy to exhaust during heavy testing or
  a long demo session with lots of free-text turns — every call
  degrades gracefully to a static fallback when that happens (never a
  crash or a stuck request), but a live demo can silently lose the
  "smart" NLU/upsell-copy behavior mid-recording if the quota runs out
  first. The chat's fast-path (see above) and the fixed `UPSELL_MAP`
  reason strings both reduce how often this matters, but don't
  eliminate it — have a fresh key ready for the actual recording.
- No concurrency/load testing — idempotency and capture-rollback are
  proven correct for sequential requests (by the test suite), not for
  two requests racing the same session/order at once.
