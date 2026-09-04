"""
Covers the policy-bounded upsell (catalog.get_upsell) and the human
chat's Groq NLU turn-taking fallback (nlu.py, POST /nlu/turn).

Runs entirely in mock mode -- see conftest.py -- and never makes a
real Groq call: GROQ_API_KEY is forced empty by conftest, so
nlu.parse_turn() always takes the deterministic fallback path unless a
test explicitly monkeypatches nlu._call_groq to exercise the
sanitization logic without any network dependency.
"""

import json
import time
import uuid

from app import catalog, nlu, sessions

DEFAULT_CATEGORIES = ["electronics", "apparel", "home", "stationery"]


# ---------------------------------------------------------------------
# Helpers (mirrors test_guardrails.py's -- kept local to avoid coupling
# the two test files together)
# ---------------------------------------------------------------------

def make_human_session(client) -> str:
    resp = client.post("/session/human")
    assert resp.status_code == 200
    return resp.json()["session_id"]


def agent_session_id(client, per_tx_cap_inr=2000, daily_cap_inr=5000, allowed_categories=None) -> str:
    warrant = {
        "agent_id": "test_agent",
        "merchant_id": sessions.MERCHANT_ID,
        "per_tx_cap_inr": per_tx_cap_inr,
        "daily_cap_inr": daily_cap_inr,
        "allowed_categories": allowed_categories or DEFAULT_CATEGORIES,
        "expires_at": time.time() + 3600,
        "nonce": uuid.uuid4().hex,
    }
    signature = sessions.sign_warrant(warrant)
    resp = client.post("/session/agent", json={"warrant": warrant, "signature": signature})
    assert resp.status_code == 200, resp.text
    return resp.json()["session_id"]


def add_and_review(client, session_id, product_id, qty=1):
    resp = client.post("/cart/add", json={"session_id": session_id, "product_id": product_id, "qty": qty})
    assert resp.status_code == 200, resp.text
    client.get(f"/cart/{session_id}")
    return resp.json()


# ---------------------------------------------------------------------
# catalog.get_upsell() -- policy-bounded, unit level
# ---------------------------------------------------------------------

def test_get_upsell_allows_when_within_cap():
    upsell, blocked = catalog.get_upsell(
        "demo_merchant", "sku_001", cart_items=[{"product_id": "sku_001", "qty": 1, "price_inr": 1499}],
        exclude_ids={"sku_001"}, max_cart_total_inr=5000,
    )
    assert blocked is None
    assert upsell["product_id"] == "sku_003"


def test_get_upsell_blocks_would_exceed_cap():
    # sku_001 (1499) + its upsell target sku_003 (349) = 1848 > 1600.
    upsell, blocked = catalog.get_upsell(
        "demo_merchant", "sku_001", cart_items=[{"product_id": "sku_001", "qty": 1, "price_inr": 1499}],
        exclude_ids={"sku_001"}, max_cart_total_inr=1600,
    )
    assert upsell is None
    assert blocked == {"product_id": "sku_003", "reason": "would_exceed_cap"}


def test_get_upsell_blocks_oos_target():
    catalog.set_stock_for_tests("demo_merchant", "sku_003", 0)
    upsell, blocked = catalog.get_upsell("demo_merchant", "sku_001", cart_items=[], exclude_ids=set())
    assert upsell is None
    assert blocked == {"product_id": "sku_003", "reason": "oos"}


def test_get_upsell_blocks_already_in_cart():
    upsell, blocked = catalog.get_upsell("demo_merchant", "sku_001", cart_items=[], exclude_ids={"sku_003"})
    assert upsell is None
    assert blocked == {"product_id": "sku_003", "reason": "already_in_cart"}


def test_get_upsell_no_mapping_is_not_a_block():
    upsell, blocked = catalog.get_upsell("demo_merchant", "sku_does_not_exist", cart_items=[], exclude_ids=set())
    assert upsell is None
    assert blocked is None


def test_get_upsell_falls_back_to_static_reason_when_llm_key_absent():
    # conftest forces GROQ_API_KEY="" -- upsell_copy.generate_reason()
    # must fall back to the fixed UPSELL_MAP string, not raise or block.
    upsell, blocked = catalog.get_upsell("demo_merchant", "sku_001", cart_items=[], exclude_ids=set(), max_cart_total_inr=5000)
    assert blocked is None
    assert upsell["reason"] == "Frequently bought with Wireless Earbuds Pro -- stay hydrated on the go."


# ---------------------------------------------------------------------
# Upsell blocking end to end -- logged and counted in metrics
# ---------------------------------------------------------------------

def test_upsell_blocked_by_cap_logged_and_counted(client):
    session_id = agent_session_id(client, per_tx_cap_inr=1600, daily_cap_inr=5000)
    resp = client.post("/cart/add", json={"session_id": session_id, "product_id": "sku_001", "qty": 1})
    assert resp.status_code == 200
    assert resp.json()["upsell"] is None

    entries = client.get("/audit-trail", params={"session_id": session_id}).json()["entries"]
    blocked_entry = next(e for e in entries if e["action"] == "upsell_blocked")
    assert blocked_entry["status"] == "blocked"
    details = json.loads(blocked_entry["details"])
    assert details == {"candidate_product_id": "sku_003", "reason": "would_exceed_cap"}

    metrics = client.get("/metrics").json()
    assert metrics["upsell_blocked_by_cap_count"] == 1
    assert metrics["upsell_shown_count"] == 0


def test_upsell_shown_when_within_agent_cap(client):
    session_id = agent_session_id(client, per_tx_cap_inr=2000, daily_cap_inr=5000)
    resp = client.post("/cart/add", json={"session_id": session_id, "product_id": "sku_001", "qty": 1})
    assert resp.status_code == 200
    assert resp.json()["upsell"]["product_id"] == "sku_003"

    metrics = client.get("/metrics").json()
    assert metrics["upsell_shown_count"] == 1
    assert metrics["upsell_blocked_by_cap_count"] == 0


def test_upsell_blocked_by_human_max_order(client):
    session_id = make_human_session(client)
    # Push the cart near guardrails.MAX_ORDER_INR (10,000) so the next
    # upsell candidate would tip it over -- sku_002's upsell target is
    # sku_004 (Rs.249); buy enough sku_002s that 249 more would exceed.
    resp = client.post("/cart/add", json={"session_id": session_id, "product_id": "sku_002", "qty": 17})  # 17*599=10183
    assert resp.status_code == 200
    assert resp.json()["upsell"] is None

    entries = client.get("/audit-trail", params={"session_id": session_id}).json()["entries"]
    blocked_entry = next(e for e in entries if e["action"] == "upsell_blocked")
    assert json.loads(blocked_entry["details"])["reason"] == "would_exceed_cap"


# ---------------------------------------------------------------------
# nlu.py -- output sanitization (no network; _call_groq monkeypatched)
# ---------------------------------------------------------------------

def test_nlu_no_api_key_always_falls_back():
    # conftest forces GROQ_API_KEY="" for the whole test session.
    plan = nlu.parse_turn("demo_merchant", "add the bottle and the earbuds")
    assert plan == {"tool": "clarify", "message": nlu.FALLBACK_MESSAGE}


def test_nlu_strips_banned_price_and_decision_fields(monkeypatch):
    monkeypatch.setattr(nlu, "GROQ_API_KEY", "fake_key_for_test")
    monkeypatch.setattr(nlu, "_call_groq", lambda merchant_id, text: {"tool": "checkout", "amount_inr": 999, "allow": True})
    plan = nlu.parse_turn("demo_merchant", "pay now")
    assert plan["tool"] == "checkout"
    assert "amount_inr" not in plan
    assert "allow" not in plan


def test_nlu_rejects_tool_outside_allowed_set(monkeypatch):
    monkeypatch.setattr(nlu, "GROQ_API_KEY", "fake_key_for_test")
    monkeypatch.setattr(nlu, "_call_groq", lambda merchant_id, text: {"tool": "issue_refund"})
    plan = nlu.parse_turn("demo_merchant", "give me a refund")
    assert plan["tool"] == "clarify"


def test_nlu_add_filters_out_invalid_product_ids(monkeypatch):
    monkeypatch.setattr(nlu, "GROQ_API_KEY", "fake_key_for_test")
    monkeypatch.setattr(nlu, "_call_groq", lambda merchant_id, text: {
        "tool": "add",
        "items": [{"product_id": "sku_003", "qty": 1}, {"product_id": "not_a_real_sku", "qty": 1}],
    })
    plan = nlu.parse_turn("demo_merchant", "add the bottle and something made up")
    assert plan["tool"] == "add"
    assert plan["items"] == [{"product_id": "sku_003", "qty": 1}]


def test_nlu_add_with_no_valid_items_falls_back(monkeypatch):
    monkeypatch.setattr(nlu, "GROQ_API_KEY", "fake_key_for_test")
    monkeypatch.setattr(nlu, "_call_groq", lambda merchant_id, text: {"tool": "add", "items": [{"product_id": "not_real"}]})
    plan = nlu.parse_turn("demo_merchant", "add the thingamajig")
    assert plan["tool"] == "clarify"


def test_nlu_browse_price_ceiling_comes_from_raw_text_regex_not_llm(monkeypatch):
    monkeypatch.setattr(nlu, "GROQ_API_KEY", "fake_key_for_test")
    # LLM deliberately returns no price info -- the ceiling below must
    # still show up, proving it came from THIS module's regex over the
    # raw text, never from the model's own output.
    monkeypatch.setattr(nlu, "_call_groq", lambda merchant_id, text: {"tool": "browse", "category": None})
    plan = nlu.parse_turn("demo_merchant", "got anything for gym under 400")
    assert plan["tool"] == "browse"
    assert plan["price_ceiling_inr"] == 400.0


def test_nlu_strips_think_block_before_parsing_json(monkeypatch):
    """Regression test -- found via live testing against a
    reasoning-variant Groq model that wraps its answer in
    <think>...</think> before the actual JSON, which naive json.loads()
    can't parse. _call_groq() must strip that block first.

    Deliberately uses text that does NOT match _try_fast_path's exact
    phrasings (unlike bare "pay") -- otherwise the fast path would
    short-circuit before ever reaching _call_groq, and this test would
    pass without exercising the code it's meant to cover at all."""
    monkeypatch.setattr(nlu, "GROQ_API_KEY", "fake_key_for_test")
    assert nlu._try_fast_path("I'd like to complete my purchase now") is None

    class _FakeResp:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"choices": [{"message": {
                "content": '\n<think>\nThe user wants to complete their purchase.\n</think>\n\n{"tool":"checkout"}',
            }}]}

    monkeypatch.setattr(nlu.requests, "post", lambda *a, **k: _FakeResp())
    plan = nlu.parse_turn("demo_merchant", "I'd like to complete my purchase now")
    assert plan["tool"] == "checkout"


def test_nlu_browse_rejects_invalid_category(monkeypatch):
    monkeypatch.setattr(nlu, "GROQ_API_KEY", "fake_key_for_test")
    monkeypatch.setattr(nlu, "_call_groq", lambda merchant_id, text: {"tool": "browse", "category": "not_a_real_category"})
    plan = nlu.parse_turn("demo_merchant", "show me stuff")
    assert plan["category"] is None


# ---------------------------------------------------------------------
# POST /nlu/turn -- executes plans through the REAL cart/checkout
# functions, never touches payments.py itself
# ---------------------------------------------------------------------

def test_nlu_turn_add_plan_shows_real_upsell(client, monkeypatch):
    session_id = make_human_session(client)
    monkeypatch.setattr(nlu, "GROQ_API_KEY", "fake_key_for_test")
    monkeypatch.setattr(nlu, "_call_groq", lambda merchant_id, text: {
        "tool": "add", "items": [{"product_id": "sku_001", "qty": 1}],
    })
    resp = client.post("/nlu/turn", json={"session_id": session_id, "text": "add the earbuds"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["tool"] == "add"
    assert body["data"][0]["upsell"]["product_id"] == "sku_003"
    # Went through the real add_to_cart() -- audit trail proves it.
    entries = client.get("/audit-trail", params={"session_id": session_id}).json()["entries"]
    assert any(e["action"] == "add_to_cart" and e["status"] == "ok" for e in entries)


def test_nlu_turn_add_multiple_items_one_turn(monkeypatch, client):
    """Demo phrase: "add the bottle and the earbuds" -- one NLU turn,
    two items added via the same add_to_cart() function twice."""
    session_id = make_human_session(client)
    monkeypatch.setattr(nlu, "GROQ_API_KEY", "fake_key_for_test")
    monkeypatch.setattr(nlu, "_call_groq", lambda merchant_id, text: {
        "tool": "add", "items": [{"product_id": "sku_003", "qty": 1}, {"product_id": "sku_001", "qty": 1}],
    })
    resp = client.post("/nlu/turn", json={"session_id": session_id, "text": "add the bottle and the earbuds"})
    assert resp.status_code == 200
    cart_resp = client.get(f"/cart/{session_id}")
    product_ids = {li["product_id"] for li in cart_resp.json()["cart"]}
    assert product_ids == {"sku_001", "sku_003"}


def test_nlu_turn_checkout_plan_goes_through_real_checkout_and_gate(client, monkeypatch):
    """Demo phrase: "pay" -- maps to the checkout tool, executed via the
    real /checkout function, so it's still gated on a prior cart review."""
    session_id = make_human_session(client)
    client.post("/cart/add", json={"session_id": session_id, "product_id": "sku_001", "qty": 1})
    # Deliberately no GET /cart -- cart_not_reviewed should still block.
    monkeypatch.setattr(nlu, "GROQ_API_KEY", "fake_key_for_test")
    monkeypatch.setattr(nlu, "_call_groq", lambda merchant_id, text: {"tool": "checkout"})

    blocked_resp = client.post("/nlu/turn", json={"session_id": session_id, "text": "pay"})
    assert blocked_resp.status_code == 200  # /nlu/turn itself always 200s -- errors come back as a reply
    assert "cart_not_reviewed" in blocked_resp.json()["reply"]

    client.get(f"/cart/{session_id}")
    ok_resp = client.post("/nlu/turn", json={"session_id": session_id, "text": "pay"})
    body = ok_resp.json()
    assert body["tool"] == "checkout"
    assert "payment_link" in body["data"]


def test_nlu_turn_agent_session_cannot_checkout_via_chat(client, monkeypatch):
    session_id = agent_session_id(client)
    monkeypatch.setattr(nlu, "GROQ_API_KEY", "fake_key_for_test")
    monkeypatch.setattr(nlu, "_call_groq", lambda merchant_id, text: {"tool": "checkout"})
    resp = client.post("/nlu/turn", json={"session_id": session_id, "text": "pay"})
    assert resp.status_code == 200
    assert resp.json()["tool"] == "clarify"


def test_nlu_turn_remove_intent_removes_the_item(client, monkeypatch):
    """Demo phrase: "that's too much, remove the earbuds" -- `remove` is
    a real tool now (gap closed), executed through the actual
    cart.remove_from_cart(), not a hardcoded reply."""
    session_id = make_human_session(client)
    client.post("/cart/add", json={"session_id": session_id, "product_id": "sku_001", "qty": 1})
    monkeypatch.setattr(nlu, "GROQ_API_KEY", "fake_key_for_test")
    monkeypatch.setattr(nlu, "_call_groq", lambda merchant_id, text: {
        "tool": "remove", "items": [{"product_id": "sku_001"}],
    })
    resp = client.post("/nlu/turn", json={"session_id": session_id, "text": "that's too much, remove the earbuds"})
    assert resp.status_code == 200
    assert resp.json()["tool"] == "remove"

    cart_resp = client.get(f"/cart/{session_id}")
    assert cart_resp.json()["cart"] == []


def test_nlu_turn_requires_valid_session(client):
    resp = client.post("/nlu/turn", json={"session_id": "not_a_real_session", "text": "pay"})
    assert resp.status_code == 401


# ---------------------------------------------------------------------
# Fast-path -- common intents skip Groq entirely (quota mitigation)
# ---------------------------------------------------------------------

def test_fast_path_matches_never_call_groq(monkeypatch):
    calls = []
    monkeypatch.setattr(nlu, "GROQ_API_KEY", "fake_key_for_test")
    monkeypatch.setattr(nlu, "_call_groq", lambda merchant_id, text: calls.append(text) or {"tool": "clarify"})

    for text in ("pay", "checkout", "cart", "my cart", "catalog", "browse"):
        plan = nlu.parse_turn("demo_merchant", text)
        assert plan["tool"] in ("checkout", "view_cart", "browse")
    assert calls == []  # Groq never touched for any of these


def test_fast_path_does_not_intercept_ambiguous_text(monkeypatch):
    monkeypatch.setattr(nlu, "GROQ_API_KEY", "fake_key_for_test")
    called = []
    monkeypatch.setattr(nlu, "_call_groq", lambda merchant_id, text: called.append(text) or {"tool": "clarify"})
    nlu.parse_turn("demo_merchant", "got anything for gym under 400")
    assert called == ["got anything for gym under 400"]


# ---------------------------------------------------------------------
# POST /cart/remove
# ---------------------------------------------------------------------

def test_cart_remove_endpoint(client):
    session_id = make_human_session(client)
    client.post("/cart/add", json={"session_id": session_id, "product_id": "sku_001", "qty": 1})
    client.post("/cart/add", json={"session_id": session_id, "product_id": "sku_003", "qty": 1})

    resp = client.post("/cart/remove", json={"session_id": session_id, "product_id": "sku_001"})
    assert resp.status_code == 200
    product_ids = {li["product_id"] for li in resp.json()["cart"]}
    assert product_ids == {"sku_003"}


def test_cart_remove_missing_item_returns_404(client):
    session_id = make_human_session(client)
    resp = client.post("/cart/remove", json={"session_id": session_id, "product_id": "sku_001"})
    assert resp.status_code == 404


def test_cart_remove_clears_review_flag(client):
    """Removing is a mutation too -- a checkout right after a remove
    (without a fresh review) must still be gated."""
    session_id = make_human_session(client)
    client.post("/cart/add", json={"session_id": session_id, "product_id": "sku_001", "qty": 1})
    client.post("/cart/add", json={"session_id": session_id, "product_id": "sku_003", "qty": 1})
    client.get(f"/cart/{session_id}")  # reviewed

    client.post("/cart/remove", json={"session_id": session_id, "product_id": "sku_001"})  # mutates again

    resp = client.post("/checkout", json={"session_id": session_id, "idempotency_key": "remove-gate-test"})
    assert resp.status_code == 403
    assert "cart_not_reviewed" in resp.json()["detail"]


# ---------------------------------------------------------------------
# Daily cap gap -- pending (created but not yet captured/failed)
# orders count too, not just captured ones
# ---------------------------------------------------------------------

def test_pending_spend_today_counts_orders_stuck_mid_flight(client):
    from app import orders as orders_module

    # A real BuyerSession row must exist first -- orders.session_id is a
    # genuine foreign key now (db.py), not a free-form string the way
    # the old orders.db was.
    stuck_session_id = agent_session_id(client)

    order_id = orders_module.new_order_id()
    orders_module.create_order(
        order_id, "demo_merchant", stuck_session_id, "ai_agent_mcp",
        [{"product_id": "sku_001", "qty": 1, "price_inr": 1499}],
        1499.0, "idem-key-1", {"order_id": order_id}, razorpay_order_id="order_stuck_mid_flight",
    )
    # Order is left in "created" status -- simulates a process crash
    # between order-creation and self-capture.
    assert orders_module.pending_spend_today("demo_merchant", "ai_agent_mcp") == 1499.0
    assert orders_module.pending_spend_today("demo_merchant", "human_whatsapp") == 0.0


def test_stuck_order_counts_toward_daily_cap_at_next_pay_attempt(client):
    from app import orders as orders_module

    session_id = agent_session_id(client, per_tx_cap_inr=2000, daily_cap_inr=1600)
    # A second, otherwise-unrelated real session -- orders.session_id is
    # a genuine foreign key now, so the "stuck" order needs a real
    # BuyerSession row to point at, even though its own pay attempt
    # never happens in this test.
    stuck_session_id = agent_session_id(client, per_tx_cap_inr=2000, daily_cap_inr=1600)

    # Simulate an order from earlier today that got stuck in "created"
    # (e.g. a process crash right after order-creation, before
    # self-capture) -- this must still count against today's cap, even
    # though it was never captured.
    stuck_id = orders_module.new_order_id()
    orders_module.create_order(
        stuck_id, "demo_merchant", stuck_session_id, "ai_agent_mcp",
        [{"product_id": "sku_001", "qty": 1, "price_inr": 1499}],
        1499.0, "stuck-idem-key", {"order_id": stuck_id}, razorpay_order_id="order_stuck",
    )

    # New attempt: Rs.349, individually well under the Rs.2,000 per-tx
    # cap -- but 1,499 (stuck) + 349 = 1,848 > 1,600 daily cap.
    add_and_review(client, session_id, "sku_003", qty=1)
    resp = client.post("/agent/pay", json={
        "session_id": session_id, "idempotency_key": "new-attempt", "confirm": True,
    })
    assert resp.status_code == 403
    assert "daily_spending_cap_exceeded" in resp.json()["detail"]
