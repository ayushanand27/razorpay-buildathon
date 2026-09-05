# 5-minute pitch video outline

Timed to fit the buildathon's 5-min cap. Pulls specific beats from
`DEMO_SCRIPT.md` rather than replacing it -- that file stays the full
reference for anyone who wants to go deeper after watching.

| Time | Beat | What to show |
|---|---|---|
| 0:00-0:25 | **Why** | One merchant backend, two buyers: a human on WhatsApp-style chat, and an AI agent via MCP -- same cart/catalog/audit path for both. Frame it against Razorpay+NPCI's real agentic-UPI pilot (Zomato/Swiggy/Zepto, Feb 2026) and NPCI's Unified Agent Protocol direction (consent + registered spending cap, not per-transaction OTP) -- this is that pattern, working. |
| 0:25-1:10 | **Human buyer** | `DEMO_SCRIPT.md` section 3 -- web chat: catalog -> add -> cart review -> checkout -> real Razorpay Payment Link. Show the link, don't necessarily pay it live yet (save the real webhook proof for the end). |
| 1:10-1:50 | **One bounded/gated block** | Section 4(c) only -- agent tries to spend over the per-transaction cap, gets a `403 blocked_by_policy`, then `explain_last_block()` showing the full decision JSON. Skip 4(b) out-of-stock -- same "bounded" point, less specific to the track's framing. |
| 1:50-2:35 | **The one deliberate failure, handled gracefully** | Section 4(d) -- `simulate_failure=true`, a real Razorpay 400 on the first attempt, automatic retry, recovery logged. This is explicitly in the track's judging bar -- say that out loud. |
| 2:35-3:00 | **Audit trail** | `web_chat/audit-dashboard.html` -- point at a few rows (checkout_attempt, policy_decision, payment_confirmed) and say "every action, any actor, explainable after the fact." Quick, don't linger. |
| 3:00-4:00 | **AI buyer via MCP** | This is the thesis of the whole project -- give it the most time. Section 6: an MCP client (Claude Desktop or similar) autonomously browsing the catalog and buying something, same guardrails firing live. |
| 4:00-4:30 | **Real webhook, live** | Pay the human-rail link from earlier (or a fresh one) for real, cut to ngrok's inspector (`http://127.0.0.1:4040`) showing the actual `POST /webhook/razorpay` landing with a real signature, then back to the audit dashboard showing `payment_confirmed`/`paid` appear. This is strong, concrete proof -- not mocked. |
| 4:30-4:50 | **Metrics** | `web_chat/metrics.html` -- captured revenue, conversion rate, upsell acceptance, computed live off the same audit trail. 10-15 sec, no more. |
| 4:50-5:00 | **Close** | One sentence: two front doors, one accountable backend, nothing hardcoded per-merchant (`POST /merchants` provisions a new one at runtime). |

## Before recording

- Fresh clean-room run once: new venv, `pip install -r requirements.txt`,
  `pytest` (should show 115 passed), then the outline above end to end --
  catches a "works on my machine, not actually committed" surprise before
  it happens on camera.
- Decide up front whether you're recording on Windows or Mac/Linux and
  don't switch mid-take -- `DEMO_SCRIPT.md`'s venv line already covers
  both, just pick one.
- If `GROQ_API_KEY` isn't set when you record, the upsell reason text
  will be the static fallback string, not LLM-generated -- that's fine,
  just narrate it as "falls back to a fixed reason without a key,
  LLM-written with one" rather than implying it's always dynamic.
