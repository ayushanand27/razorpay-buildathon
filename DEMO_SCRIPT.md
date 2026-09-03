# Demo Script

Copy-paste-ready commands for the live recording, in run order. All commands
are taken directly from README.md; nothing here is new syntax.

## 1. Start the backend

```bash
cd backend
venv\Scripts\activate
uvicorn app.main:app --port 8123
```

Confirm: open a second terminal and check
```bash
curl http://127.0.0.1:8123/health
```
Expect: `{"status":"ok"}`

## 2. Human buyer demo (WhatsApp-style web chat)

Open `web_chat/index.html` in a browser (backend must already be running).

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

## 3. The 4 guardrail / checkout scenarios (curl)

Same requests as the human/AI buyer flows above, run directly against the
API for a fast, narrated pass — useful if clicking through the chat UI is
too slow for the video.

**a. Happy-path checkout (real Razorpay test-mode link)**
```bash
curl -X POST http://127.0.0.1:8123/cart/add -H "Content-Type: application/json" -d "{\"session_id\":\"demo1\",\"actor\":\"human_whatsapp\",\"product_id\":\"sku_001\",\"qty\":1}"
curl -X POST http://127.0.0.1:8123/checkout -H "Content-Type: application/json" -d "{\"session_id\":\"demo1\",\"actor\":\"human_whatsapp\"}"
```
Expect: a real `https://rzp.io/...` payment_link.

**b. Out-of-stock guardrail block**
```bash
curl -X POST http://127.0.0.1:8123/cart/add -H "Content-Type: application/json" -d "{\"session_id\":\"demo2\",\"actor\":\"ai_agent_mcp\",\"product_id\":\"sku_005\",\"qty\":1}"
curl -X POST http://127.0.0.1:8123/checkout -H "Content-Type: application/json" -d "{\"session_id\":\"demo2\",\"actor\":\"ai_agent_mcp\"}"
```
Expect: `403`, `blocked_by_guardrail: out_of_stock`.

**c. AI spending-cap guardrail block (cart total over ₹2,000)**
```bash
curl -X POST http://127.0.0.1:8123/cart/add -H "Content-Type: application/json" -d "{\"session_id\":\"demo3\",\"actor\":\"ai_agent_mcp\",\"product_id\":\"sku_001\",\"qty\":2}"
curl -X POST http://127.0.0.1:8123/checkout -H "Content-Type: application/json" -d "{\"session_id\":\"demo3\",\"actor\":\"ai_agent_mcp\"}"
```
Expect: `403`, `blocked_by_guardrail: amount_inr 2998 exceeds AI agent spending cap of 2000`.

**d. simulate_failure — graceful retry and recovery**
```bash
curl -X POST http://127.0.0.1:8123/cart/add -H "Content-Type: application/json" -d "{\"session_id\":\"demo4\",\"actor\":\"human_whatsapp\",\"product_id\":\"sku_003\",\"qty\":1}"
curl -X POST http://127.0.0.1:8123/checkout -H "Content-Type: application/json" -d "{\"session_id\":\"demo4\",\"actor\":\"human_whatsapp\",\"simulate_failure\":true}"
```
Expect: `200`, `note` field contains `recovered_after_retry`.

## 4. Audit trail — show every action logged

```bash
curl http://127.0.0.1:8123/audit-trail
```
Or open in a browser: `http://127.0.0.1:8123/audit-trail`

Point out: every action above (add/checkout/blocked/retried) appears with
actor, amount, and status — nothing from either flow is missing.

## 5. AI buyer demo (MCP)

In a separate terminal (keep the backend from step 1 running):
```bash
cd mcp_server
venv\Scripts\activate
```
Point your MCP client's config at `server.py` — use
`mcp_client_config.example.json` as-is (paths are already filled in for
this machine). Restart the client, then ask it:

```
browse the demo merchant's catalog and buy me a water bottle
```

It will call `browse_catalog`, `add_to_cart`, `view_cart`, and `checkout`
live, hitting the exact same backend as the WhatsApp flow in step 2.

## 6. Upsell demo and Growth Metrics walkthrough

Back in the human-buyer chat (step 2), after `add sku_001`, watch for the
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
curl -X POST http://127.0.0.1:8123/cart/add -H "Content-Type: application/json" -d "{\"session_id\":\"demo5\",\"actor\":\"human_whatsapp\",\"product_id\":\"sku_001\",\"qty\":1}"
```
Expect: the response includes an `"upsell"` object suggesting `sku_003`.

Now open `web_chat/metrics.html` (same nav strip as the other three
pages). Auto-refreshing every 5s, same as the audit dashboard, it shows:
- Total Revenue
- Revenue by Actor (human vs AI, a simple CSS bar comparison)
- Conversion Rate (overall and split by actor)
- Upsell Acceptance Rate

Point out: every number on this page comes from `GET /metrics`, which
reads straight off the same audit trail shown in step 4 -- there is no
separate metrics log, just a live aggregation over data the system was
already writing.
