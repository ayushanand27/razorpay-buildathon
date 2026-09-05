# 5-minute pitch video — recording guide

## Part 1: What you actually built (say this in your own words, don't read it)

Forget the code for a second. Here's the plain-English version of the
whole project:

**The problem**: today, a small merchant's website or WhatsApp bot is
built for a HUMAN to click through. An AI agent (a shopping assistant,
a personal AI, whatever) has no way to browse that merchant's catalog
or actually complete a purchase on someone's behalf. Small merchants
are invisible to the AI-agent economy that's starting to show up
(Razorpay + NPCI already piloting exactly this with Zomato/Swiggy/Zepto).

**What you built**: one backend, ONE merchant, but TWO front doors in:
a human buyer (simulated WhatsApp chat) and an AI buyer (an MCP server
any AI agent — you're using Claude — can plug into). Both go through
the literal same code for catalog, cart, and audit logging. They only
split apart at the very last step — how they actually pay — because a
human has a browser to open a payment link in, and an AI agent doesn't:

- **Human** → Razorpay **Payment Links** (they open a link, pay it
  like a normal checkout)
- **AI agent** → Razorpay **Orders API**, completed immediately,
  because there's no browser to hand a link to

**Why an AI agent can be trusted to spend money at all**: it isn't
trusted blindly. Before it's allowed to mint a session, it has to
present a **signed spending warrant** — a cryptographically signed
document saying "this agent may spend up to ₹X per transaction, ₹Y per
day, only in these categories, expiring at this time." Every single
payment attempt re-checks that warrant from scratch — signature,
expiry, category, price match, per-transaction cap, AND a running
daily total across every transaction that agent has made — before
anything is allowed to happen. This is deliberately modeled on what
NPCI's own Unified Agent Protocol is proposing: consent + a registered
spending limit, instead of a one-time OTP per purchase (which doesn't
make sense when there's no human there to receive an OTP).

**Why every action is logged**: every single thing either buyer does —
add to cart, checkout attempt, blocked purchase, successful payment —
gets written to an audit trail with who did it, how much, and why it
was allowed or blocked. This is what makes the system "explainable" —
you can point at any purchase (or blocked attempt) and show exactly
why it happened.

**The one deliberate failure**: to prove the system doesn't just work
in the happy path, you built one intentionally broken payment attempt
(a real invalid API call to Razorpay) that gets a real error back, then
automatically retries with the fix and succeeds — logging both the
failure and the recovery.

**What's real vs. simulated**: the Razorpay payments are REAL (test
mode — no real money, but the actual Razorpay API, actual payment
links, actual webhook signatures). WhatsApp itself is simulated as a
web chat (swapping in the real WhatsApp Business API later is a
frontend change, not a backend one). The AI agent is a real Claude
instance talking over the real MCP protocol, not a scripted fake.

That's it. That's the whole pitch. Everything else is proving those
claims live.

---

## Part 2: Recording setup (do this before you hit record)

1. **Pick ONE OS and stick to it.** Don't switch between Windows and
   Mac/Linux commands mid-recording.
2. **Clean-room run once, before recording for real**: fresh venv,
   `pip install -r backend/requirements.txt`, run `pytest` in
   `backend/` (expect `115 passed`), then walk the script below once
   end-to-end without recording, just to catch anything that's
   different on a fresh checkout.
3. **Decide on revenue/stock state**: if you want `web_chat/metrics.html`
   to show ₹0 before you start (cleaner for camera), delete
   `backend/app/app.db` and restart the backend right before recording
   — it reseeds automatically. If you'd rather show it already has
   real captured revenue from testing, leave it.
4. **Windows to have open, in this order**, so you can Alt-Tab/switch
   without fumbling:
   - Terminal running the backend (`uvicorn app.main:app ...`)
   - Terminal running `ngrok http 8123` (only if doing the live
     webhook beat)
   - Browser tab 1: `web_chat/index.html` (chat)
   - Browser tab 2: `web_chat/audit-dashboard.html`
   - Browser tab 3: `web_chat/metrics.html`
   - Browser tab 4: `http://127.0.0.1:4040` (ngrok inspector — only if
     doing the live webhook beat)
   - Your MCP client (Claude Desktop or similar) already connected to
     `mcp_server/server.py`
5. **If `GROQ_API_KEY` isn't set**, the upsell text will be a fixed
   string, not LLM-generated. Fine — just don't claim it's dynamic on
   camera if it isn't actually on.
6. Do a **test recording of just audio** first if you can — mic issues
   are the #1 reason people re-record a whole 5-minute take.

---

## Part 3: The script (timed — read the "say" column as a guide, not verbatim)

| Time | Say (roughly) | Show |
|---|---|---|
| **0:00–0:25** | "Small merchants aren't set up for AI buyers today — an AI agent has no way to browse their catalog or pay on someone's behalf. This is one merchant backend, transactable by both a human and an AI agent, end to end — modeled on the pattern Razorpay and NPCI are already piloting with Zomato, Swiggy, and Zepto." | Just you / title slide, no screen yet |
| **0:25–1:10** | "Here's the human side — a WhatsApp-style chat." Type `catalog`, `add sku_001`, `cart`, `checkout`. "That's a real Razorpay Payment Link — not a mock." | `index.html` — do the flow live |
| **1:10–1:50** | "Now the AI side has its own rules — a signed spending warrant, re-checked on every attempt. Watch what happens if it tries to spend past its cap." Trigger the per-transaction cap block (`DEMO_SCRIPT.md` 4c). "It's blocked, and it can ask exactly why." | Terminal — the curl calls, or narrate over a pre-run result |
| **1:50–2:35** | "One thing I built deliberately: a real payment failure, handled gracefully." Trigger `simulate_failure=true` (4d). "That first attempt actually failed against Razorpay's real API — a genuine 400 — and the system retried and recovered automatically, logging both." | Terminal / audit dashboard |
| **2:35–3:00** | "Every one of those actions — human or AI, allowed or blocked — is in one audit trail." Scroll it briefly. | `audit-dashboard.html` |
| **3:00–4:00** | "This is the real thesis of the project — the same merchant, bought from by an actual AI agent." Ask your MCP-connected agent to browse and buy something. Let it run live. | Your MCP client (Claude) |
| **4:00–4:30** | "And this isn't simulated — here's a real Razorpay webhook landing." Pay a link for real, cut to the ngrok inspector showing the actual signed `POST /webhook/razorpay`, then back to the audit trail showing `payment_confirmed / paid` appear. | `127.0.0.1:4040` → `audit-dashboard.html` |
| **4:30–4:50** | "And all of that rolls up into live growth metrics — captured revenue, conversion, upsell acceptance." | `metrics.html` |
| **4:50–5:00** | "One backend, two accountable front doors, and a new merchant can be added at runtime with zero redeploy. Thanks." | Back to you |

## If something breaks live

- Don't panic-narrate the bug — say "let me show that from the test
  suite instead" and point at the matching test in
  `backend/tests/` (they all have descriptive names for exactly this).
- If Razorpay's webhook is slow to arrive, that's normal (seconds, not
  instant) — keep talking over it rather than sitting in silence.
