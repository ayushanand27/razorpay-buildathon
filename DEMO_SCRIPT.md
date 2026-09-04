# Demo Script

Copy-paste-ready commands for the live recording, in run order. All commands
are taken directly from README.md; nothing here is new syntax.

## 1. Start the backend

```bash
cd backend
source venv/bin/activate   # Windows: venv\Scripts\activate
uvicorn app.main:app --port 8123
```

Confirm: open a second terminal and check
```bash
curl http://127.0.0.1:8123/health
```
Expect: `{"status":"ok"}`

## 2. Run the automated test suite

```bash
cd backend
source venv/bin/activate   # Windows: venv\Scripts\activate
pytest
```
Expect: all tests pass, entirely in mock mode (no keys, no network) --
`backend/tests/test_guardrails.py` covers every scenario in section 4
below, both rails, plus the price-tamper, expired-warrant-at-pay-time,
category, pay-without-confirm, idempotency, and stock-invariant
guarantees.

## 3. Human buyer demo (WhatsApp-style web chat)

Open `web_chat/index.html` in a browser (backend must already be running).
On load, the page calls `POST /session/human` automatically to get a
session id -- there's nothing to configure.

Type these one at a time in the chat box, or click the matching quick button:
```
catalog
add sku_001
cart
checkout
```

Then, separately, to show the graceful-failure-and-retry path:
```
fail demo
```

## 4. Two rails, one policy engine (curl)

**Split rails**: a human pays via a Razorpay Payment Link they open
themselves (`POST /checkout`); an AI agent has no browser to open a
link in, so it pays via the Razorpay Orders API, self-completed
immediately (`POST /agent/pay`). `POST /checkout` now rejects an agent
session and `POST /agent/pay` rejects a human one -- each rail is
enforced server-side, not just a client-side convention.

**Actor is never sent in the request body.** Every call below first
gets a `session_id` from `POST /session/human` or `POST
/session/agent` -- the backend looks up who's calling from that
session (see `backend/app/sessions.py`). An AI-agent session requires
a spending warrant signed with `AGENT_WARRANT_SECRET`; this snippet
(from `backend/` with the venv active) creates one with the exact caps
this section's numbers assume and prints the resulting `session_id`:

```bash
cd backend
python -c "
import time, uuid, requests
from app.sessions import sign_warrant
warrant = {
    'agent_id': 'demo_agent', 'merchant_id': 'demo_merchant',
    'per_tx_cap_inr': 2000, 'daily_cap_inr': 5000,
    'allowed_categories': ['electronics', 'apparel', 'home', 'stationery'],
    'expires_at': time.time() + 3600, 'nonce': uuid.uuid4().hex,
}
resp = requests.post('http://127.0.0.1:8123/session/agent',
                      json={'warrant': warrant, 'signature': sign_warrant(warrant)})
print(resp.json())
"
```
**Re-run this once per AI-agent scenario below (b, c, f), not once for
all of them** -- each run mints a fresh, empty-cart session (a nonce
can't be reused anyway, so a second run always succeeds with a new
session). Reusing one `$AGENT_SESSION` across scenarios would leave a
BLOCKED scenario's items sitting in the cart for the next one (a
blocked pay attempt never clears the cart, only a successful one
does), contaminating the next scenario's total. Copy each run's
printed `session_id` into `$AGENT_SESSION` fresh before that scenario.

For a human session, just:
```bash
curl -X POST http://127.0.0.1:8123/session/human
```
and copy that `session_id` into `$HUMAN_SESSION` (fresh per scenario,
same reasoning).

Every scenario views the cart (`GET /cart/{session_id}`) before paying
-- that's the "gated" guarantee from scenario (e): a mutation clears
any prior review, so a fresh review is required after every add. Every
checkout/pay call also carries a required `idempotency_key` (Task 2)
-- any string, unique per attempt. Agent-rail calls also require
`confirm: true`.

**a. Human happy-path checkout + capture (real Razorpay test-mode Payment Link)**
```bash
curl -X POST http://127.0.0.1:8123/cart/add -H "Content-Type: application/json" -d "{\"session_id\":\"$HUMAN_SESSION\",\"product_id\":\"sku_001\",\"qty\":1}"
curl http://127.0.0.1:8123/cart/$HUMAN_SESSION
curl -X POST http://127.0.0.1:8123/checkout -H "Content-Type: application/json" -d "{\"session_id\":\"$HUMAN_SESSION\",\"idempotency_key\":\"demo-a-1\"}"
```
Expect: a real `https://rzp.io/...` payment_link -- but this is an
ORDER, not revenue yet (see `GET /metrics`: `orders_created_inr` goes
up, `captured_inr` doesn't). A human's payment is confirmed later, by a
real Razorpay webhook in production; to simulate that locally, sign a
capture event with the `payment_link_id` from the checkout response --
the SAME signature scheme (`RAZORPAY_WEBHOOK_SECRET`, HMAC over the
raw body) a real webhook call uses:
```bash
cd backend
python -c "
import json, requests
from app.webhooks import build_capture_payload, sign_body
payment_link_id = 'PASTE_THE_id_FIELD_FROM_THE_CHECKOUT_RESPONSE'
body = json.dumps(build_capture_payload(payment_link_id=payment_link_id, amount_inr=1499)).encode()
resp = requests.post('http://127.0.0.1:8123/demo/simulate-capture', data=body,
                      headers={'X-Razorpay-Signature': sign_body(body), 'Content-Type': 'application/json'})
print(resp.status_code, resp.json())
"
```
Now `GET /metrics` shows `captured_inr` (== `total_revenue_inr`)
including this order, and `sku_001`'s stock is down by 1.

**b. Agent rail — out-of-stock policy block**
```bash
curl -X POST http://127.0.0.1:8123/cart/add -H "Content-Type: application/json" -d "{\"session_id\":\"$AGENT_SESSION\",\"product_id\":\"sku_005\",\"qty\":1}"
curl http://127.0.0.1:8123/cart/$AGENT_SESSION
curl -X POST http://127.0.0.1:8123/agent/pay -H "Content-Type: application/json" -d "{\"session_id\":\"$AGENT_SESSION\",\"idempotency_key\":\"demo-b-1\",\"confirm\":true}"
```
Expect: `403`, `blocked_by_policy: out_of_stock`. Then:
```bash
curl "http://127.0.0.1:8123/agent/explain-last-block?session_id=$AGENT_SESSION"
```
Expect: the full policy decision JSON logged for that attempt
(`{"allow": false, "reason": "out_of_stock", "remaining_cap_inr": ...}`)
-- this is the same thing the MCP `explain_last_block()` tool surfaces
to an agent.

**c. Agent rail — per-transaction cap policy block (cart total over ₹2,000)**
```bash
curl -X POST http://127.0.0.1:8123/cart/add -H "Content-Type: application/json" -d "{\"session_id\":\"$AGENT_SESSION\",\"product_id\":\"sku_001\",\"qty\":2}"
curl http://127.0.0.1:8123/cart/$AGENT_SESSION
curl -X POST http://127.0.0.1:8123/agent/pay -H "Content-Type: application/json" -d "{\"session_id\":\"$AGENT_SESSION\",\"idempotency_key\":\"demo-c-1\",\"confirm\":true}"
```
Expect: `403`, `blocked_by_policy: amount_inr 2998 exceeds per-transaction cap of 2000`
(the cap here comes from the warrant used to create `$AGENT_SESSION`,
verified fresh by `policy.py` on every attempt).

**d. Human rail — simulate_failure: a REAL Razorpay 400, then graceful retry and recovery**
```bash
curl -X POST http://127.0.0.1:8123/cart/add -H "Content-Type: application/json" -d "{\"session_id\":\"$HUMAN_SESSION\",\"product_id\":\"sku_003\",\"qty\":1}"
curl http://127.0.0.1:8123/cart/$HUMAN_SESSION
curl -X POST http://127.0.0.1:8123/checkout -H "Content-Type: application/json" -d "{\"session_id\":\"$HUMAN_SESSION\",\"idempotency_key\":\"demo-d-1\",\"simulate_failure\":true}"
```
Expect: `200`, `note` field contains `recovered_after_retry`. Under
the hood this made a genuinely invalid first request (`amount=0`),
which Razorpay's real API genuinely rejected with a real `400` --
check `GET /audit-trail` for the `checkout_payment` entry's
`first_attempt_response` field to see that real rejection body. In
mock mode (no Razorpay keys) the same 400 JSON shape is simulated
locally, so this scenario works identically either way. (The agent
rail has no equivalent forced-failure demo path -- it's a single
Orders API call, immediately self-captured.)

**e. Human rail — gated block, checkout without reviewing the cart first**
```bash
curl -X POST http://127.0.0.1:8123/cart/add -H "Content-Type: application/json" -d "{\"session_id\":\"$HUMAN_SESSION\",\"product_id\":\"sku_001\",\"qty\":1}"
curl -X POST http://127.0.0.1:8123/checkout -H "Content-Type: application/json" -d "{\"session_id\":\"$HUMAN_SESSION\",\"idempotency_key\":\"demo-e-1\"}"
```
Expect: `403`, `blocked_by_guardrail: cart_not_reviewed`. No `GET
/cart/$HUMAN_SESSION` call happened for this session, so the
server-side gate blocks it even though the cart itself is perfectly
valid. (The identical rule, `cart_reviewed since last mutation`, is
rule 7 of `policy.py` on the agent rail -- and now also fires if the
cart is *mutated again* after a review, not just when it was never
reviewed at all.)

**f. Agent rail — cumulative daily cap, counts CAPTURED spend only**

Three separate ₹1,848 purchases (sku_001 + sku_003) from the same AI
agent (a fresh `$AGENT_SESSION`, so it starts at ₹0 captured today).
Each individually clears the ₹2,000 per-transaction cap (scenario c).
`POST /agent/pay` self-captures synchronously, so each successful call
counts toward the daily cap immediately -- no separate capture step
needed on this rail, unlike scenario (a):
```bash
# Transaction 1 -- Rs.1,848, well under the per-transaction cap
curl -X POST http://127.0.0.1:8123/cart/add -H "Content-Type: application/json" -d "{\"session_id\":\"$AGENT_SESSION\",\"product_id\":\"sku_001\",\"qty\":1}"
curl -X POST http://127.0.0.1:8123/cart/add -H "Content-Type: application/json" -d "{\"session_id\":\"$AGENT_SESSION\",\"product_id\":\"sku_003\",\"qty\":1}"
curl http://127.0.0.1:8123/cart/$AGENT_SESSION
curl -X POST http://127.0.0.1:8123/agent/pay -H "Content-Type: application/json" -d "{\"session_id\":\"$AGENT_SESSION\",\"idempotency_key\":\"demo-f-1\",\"confirm\":true}"
```
Expect: `200`, `"status": "captured"`. Today's AI-agent CAPTURED total: ₹1,848.

```bash
# Transaction 2 -- same $AGENT_SESSION, another Rs.1,848.
curl -X POST http://127.0.0.1:8123/cart/add -H "Content-Type: application/json" -d "{\"session_id\":\"$AGENT_SESSION\",\"product_id\":\"sku_001\",\"qty\":1}"
curl -X POST http://127.0.0.1:8123/cart/add -H "Content-Type: application/json" -d "{\"session_id\":\"$AGENT_SESSION\",\"product_id\":\"sku_003\",\"qty\":1}"
curl http://127.0.0.1:8123/cart/$AGENT_SESSION
curl -X POST http://127.0.0.1:8123/agent/pay -H "Content-Type: application/json" -d "{\"session_id\":\"$AGENT_SESSION\",\"idempotency_key\":\"demo-f-2\",\"confirm\":true}"
```
Expect: `200`. Captured total: ₹3,696. Check `GET
/agent/remaining-cap?session_id=$AGENT_SESSION` -- `daily_remaining_inr`
is now `1304`.

```bash
# Transaction 3 -- still individually under the per-transaction cap,
# but 3,696 + 1,848 = 5,544 exceeds the Rs.5,000 daily cap -- blocks
# at policy-evaluation time, before any order is even created.
curl -X POST http://127.0.0.1:8123/cart/add -H "Content-Type: application/json" -d "{\"session_id\":\"$AGENT_SESSION\",\"product_id\":\"sku_001\",\"qty\":1}"
curl -X POST http://127.0.0.1:8123/cart/add -H "Content-Type: application/json" -d "{\"session_id\":\"$AGENT_SESSION\",\"product_id\":\"sku_003\",\"qty\":1}"
curl http://127.0.0.1:8123/cart/$AGENT_SESSION
curl -X POST http://127.0.0.1:8123/agent/pay -H "Content-Type: application/json" -d "{\"session_id\":\"$AGENT_SESSION\",\"idempotency_key\":\"demo-f-3\",\"confirm\":true}"
```
Expect: `403`, `blocked_by_policy: daily_spending_cap_exceeded`.

(For a fast narrated pass without doing this 3 times by hand,
`backend/tests/test_guardrails.py::test_agent_daily_cap_counts_captured_only`
runs this exact sequence in under a second -- point to that instead.)

## 5. Audit trail — show every action logged

```bash
curl http://127.0.0.1:8123/audit-trail
```
Or open in a browser: `http://127.0.0.1:8123/audit-trail`

Point out: every action above appears with actor, amount, and status
-- nothing from either rail is missing. `policy_decision` entries (new)
carry the AGENT rail's full ALLOW/BLOCK decision JSON, reason, and
remaining cap for every single attempt; `checkout_attempt` /
`checkout_payment` are the coarser entries both rails share (so
`GET /metrics`'s conversion-rate numbers stay comparable across
rails); `payment_confirmed` is the capture step, separate from
`checkout_payment` (order/link creation).

## 6. AI buyer demo (MCP)

In a separate terminal (keep the backend from step 1 running):
```bash
cd mcp_server
source venv/bin/activate   # Windows: venv\Scripts\activate
python generate_mcp_config.py
```
That writes `mcp_client_config.json` with the correct absolute paths
for this machine — no hand-editing paths (see
`mcp_client_config.example.json` if you want the raw format instead).
Point your MCP client's config at the file it just wrote. Restart the
client, then ask it:

```
browse the demo merchant's catalog and buy me a water bottle
```

Sessions here are per-buyer, not fixed at server startup: the first
tool call that needs one (e.g. `add_to_cart`) signs a fresh spending
warrant (using `AGENT_WARRANT_SECRET` from `.env`) and calls `POST
/session/agent` to get authorized on the fly -- if `AGENT_WARRANT_SECRET`
isn't set, it fails fast with a clear error instead of silently
falling back to anything. Every tool response includes its
`session_id`, so the calling agent can track and reuse it across
calls (or manage several buyers' sessions concurrently by passing a
different `session_id` per buyer).

It will call `browse_catalog`, `add_to_cart`, `view_cart`, and `pay`
live, hitting the exact same backend and `policy.py` decision engine
as the curl scenarios in step 4. Two more tools are available for the
agent to use itself:
- `remaining_cap()` -- per-transaction cap, how much of the daily cap
  is left, and when the warrant expires.
- `explain_last_block()` -- the full reason the last `pay()` attempt
  was blocked, if any (mirrors `GET /agent/explain-last-block`).

The full catalog is also available as an MCP **resource**
(`merchant://catalog`), not just a tool call -- same enriched,
agent-readable fields as `GET /catalog` (sku, price, currency, tax_bps,
stock, availability, category, attributes, return_window_days).

## 7. Upsell demo and Growth Metrics walkthrough

Back in the human-buyer chat (step 3), after `add sku_001`, watch for the
💡 suggestion right below the "Added!" message:
```
💡 Stainless Steel Water Bottle — Rs.349
Frequently bought with Wireless Earbuds Pro -- stay hydrated on the go.
Reply: add sku_003
```
Reply `add sku_003` to accept it -- that's what counts as an "upsell
accepted" in the metrics below.

Or via curl, for a fast narrated pass:
```bash
curl -X POST http://127.0.0.1:8123/cart/add -H "Content-Type: application/json" -d "{\"session_id\":\"$HUMAN_SESSION\",\"product_id\":\"sku_001\",\"qty\":1}"
```
Expect: the response includes an `"upsell"` object suggesting `sku_003`.

Now open `web_chat/metrics.html` (same nav strip as the other three
pages). Auto-refreshing every 5s, same as the audit dashboard, it shows:
- **Captured Revenue** -- `payment_confirmed`, status=paid only; money
  that actually moved (Task 5), from BOTH rails combined
- **Orders Created** -- payment links (human rail) and orders (agent
  rail) created, whether or not they were ever captured; the gap
  between this and Captured Revenue is shown directly on the card
- Captured Revenue by Actor (human vs AI, a simple CSS bar comparison)
- Conversion Rate (overall and split by actor)
- Upsell Acceptance Rate

## 8. Bonus: multi-tenancy (optional, if there's time)

Everything above ran against `demo_merchant`. A second, unrelated
merchant (`fit_supply_co` -- gym equipment) is registered too, proving
the tenant boundary is real, not just one merchant behind a config
flag:
```bash
curl http://127.0.0.1:8123/merchants
curl http://127.0.0.1:8123/merchants/fit_supply_co/catalog
```
Point out: `fit_supply_co`'s `sku_001` is a completely different
product ("Adjustable Dumbbell Set") from `demo_merchant`'s `sku_001`
("Wireless Earbuds Pro") -- every lookup is scoped by
`(merchant_id, product_id)` together, never just the SKU string.

Mint an agent session there the same way as section 4, but against the
merchant-scoped endpoint and signed with `FIT_SUPPLY_WARRANT_SECRET`:
```bash
cd backend
python -c "
import time, uuid, requests
from app.sessions import sign_warrant
from app import merchants
warrant = {
    'agent_id': 'fit_demo_agent', 'merchant_id': 'fit_supply_co',
    'per_tx_cap_inr': 8000, 'daily_cap_inr': 10000,
    'allowed_categories': ['equipment', 'supplements'],
    'expires_at': time.time() + 3600, 'nonce': uuid.uuid4().hex,
}
sig = sign_warrant(warrant, secret=merchants.get_warrant_secret('fit_supply_co'))
resp = requests.post('http://127.0.0.1:8123/merchants/fit_supply_co/session/agent',
                      json={'warrant': warrant, 'signature': sig})
print(resp.json())
"
```
Copy the printed `session_id` into `$FIT_SESSION`, then buy the
dumbbells (`sku_001` at THIS merchant) exactly like any other agent
purchase:
```bash
curl -X POST http://127.0.0.1:8123/cart/add -H "Content-Type: application/json" -d "{\"session_id\":\"$FIT_SESSION\",\"product_id\":\"sku_001\",\"qty\":1}"
curl http://127.0.0.1:8123/cart/$FIT_SESSION
curl -X POST http://127.0.0.1:8123/agent/pay -H "Content-Type: application/json" -d "{\"session_id\":\"$FIT_SESSION\",\"idempotency_key\":\"demo-mt-1\",\"confirm\":true}"
```
Expect: `"status":"captured"`, real Razorpay order id, and a live
Groq upsell suggesting the Yoga Mat -- same policy engine, same
capture path, completely separate merchant and daily cap from
everything in sections 1-7.

Point out: every number on this page comes from `GET /metrics`, which
reads straight off the same audit trail shown in step 5 -- there is no
separate metrics log, just a live aggregation over data the system was
already writing.
