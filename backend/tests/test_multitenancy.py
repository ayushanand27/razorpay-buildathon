"""
Multi-tenancy -- proves the merchant boundary is real, not just
claimed: separate catalogs (including two SKUs that deliberately reuse
the SAME id across merchants), separate warrant secrets, sessions
bound to exactly one merchant, and spending caps that never leak
across the boundary.
"""

import time
import uuid

from app import merchant_registry, sessions

FIT_SUPPLY = "fit_supply_co"
DEMO = "demo_merchant"


def sign_and_mint_agent_session(client, merchant_id, per_tx_cap_inr=5000, daily_cap_inr=5000,
                                 allowed_categories=None, secret=None, nonce=None):
    warrant = {
        "agent_id": "test_agent",
        "merchant_id": merchant_id,
        "per_tx_cap_inr": per_tx_cap_inr,
        "daily_cap_inr": daily_cap_inr,
        "allowed_categories": allowed_categories or ["equipment", "supplements", "electronics",
                                                       "apparel", "home", "stationery"],
        "expires_at": time.time() + 3600,
        "nonce": nonce or uuid.uuid4().hex,
    }
    sig = sessions.sign_warrant(warrant, secret=secret or merchant_registry.get_warrant_secret(merchant_id))
    return client.post(f"/merchants/{merchant_id}/session/agent", json={"warrant": warrant, "signature": sig})


def mint_human_session(client, merchant_id):
    resp = client.post(f"/merchants/{merchant_id}/session/human")
    assert resp.status_code == 200
    return resp.json()["session_id"]


def add_and_review(client, session_id, product_id, qty=1):
    resp = client.post("/cart/add", json={"session_id": session_id, "product_id": product_id, "qty": qty})
    assert resp.status_code == 200, resp.text
    client.get(f"/cart/{session_id}")
    return resp.json()


# ---------------------------------------------------------------------
# Merchant listing and catalog isolation
# ---------------------------------------------------------------------

def test_list_merchants_returns_both(client):
    resp = client.get("/merchants")
    assert resp.status_code == 200
    ids = {m["merchant_id"] for m in resp.json()["merchants"]}
    assert ids == {DEMO, FIT_SUPPLY}


def test_unknown_merchant_catalog_404s(client):
    resp = client.get("/merchants/not_a_real_merchant/catalog")
    assert resp.status_code == 404


def test_two_merchants_reusing_the_same_sku_id_never_collide(client):
    """db.py's seed data deliberately gives demo_merchant and fit_supply_co
    both a "sku_001" -- completely different products. Fetching each
    merchant's catalog must show ITS OWN sku_001, never the other's."""
    demo_products = {p["id"]: p for p in client.get(f"/merchants/{DEMO}/catalog").json()["products"]}
    fit_products = {p["id"]: p for p in client.get(f"/merchants/{FIT_SUPPLY}/catalog").json()["products"]}

    assert demo_products["sku_001"]["name"] == "Wireless Earbuds Pro"
    assert fit_products["sku_001"]["name"] == "Adjustable Dumbbell Set (5-25kg)"
    assert demo_products["sku_001"]["price_inr"] != fit_products["sku_001"]["price_inr"]


def test_backward_compatible_catalog_alias_matches_demo_merchant(client):
    old_route = client.get("/catalog").json()["products"]
    new_route = client.get(f"/merchants/{DEMO}/catalog").json()["products"]
    assert old_route == new_route


# ---------------------------------------------------------------------
# Session <-> merchant binding
# ---------------------------------------------------------------------

def test_human_session_is_bound_to_the_merchant_it_was_minted_for(client):
    session_id = mint_human_session(client, FIT_SUPPLY)
    resp = add_and_review(client, session_id, "sku_006")  # Yoga Mat -- only exists at fit_supply_co
    assert resp["cart"][0]["name"] == "Yoga Mat -- Non-Slip 6mm"

    # The SAME product_id at demo_merchant is a completely different
    # (nonexistent, in fact) SKU -- adding it to a fit_supply_co session
    # must resolve against fit_supply_co's catalog only.
    demo_session_id = mint_human_session(client, DEMO)
    resp2 = client.post("/cart/add", json={"session_id": demo_session_id, "product_id": "sku_006", "qty": 1})
    assert resp2.status_code == 404  # demo_merchant has no sku_006


def test_old_unprefixed_session_routes_default_to_demo_merchant(client):
    resp = client.post("/session/human")
    session_id = resp.json()["session_id"]
    # sku_001 via the old routes must be demo_merchant's earbuds, not
    # fit_supply_co's dumbbells.
    add_resp = add_and_review(client, session_id, "sku_001")
    assert add_resp["cart"][0]["name"] == "Wireless Earbuds Pro"


# ---------------------------------------------------------------------
# Warrant secret isolation
# ---------------------------------------------------------------------

def test_warrant_genuinely_signed_for_one_merchant_cannot_mint_a_session_at_another(client):
    """A warrant claiming merchant_id=fit_supply_co, genuinely signed
    with fit_supply_co's OWN secret, presented to demo_merchant's
    session endpoint: verification there uses demo_merchant's secret
    (the URL-path merchant), which this warrant was never signed with
    -- so it's rejected at the SIGNATURE check, before merchant_id is
    even compared. An attacker without demo_merchant's secret can't
    mint a session there no matter which merchant their own valid
    warrant claims to be for."""
    warrant = {
        "agent_id": "test_agent", "merchant_id": FIT_SUPPLY,
        "per_tx_cap_inr": 2000, "daily_cap_inr": 5000,
        "allowed_categories": ["equipment"], "expires_at": time.time() + 3600,
        "nonce": uuid.uuid4().hex,
    }
    sig = sessions.sign_warrant(warrant, secret=merchant_registry.get_warrant_secret(FIT_SUPPLY))

    resp = client.post(f"/merchants/{DEMO}/session/agent", json={"warrant": warrant, "signature": sig})
    assert resp.status_code == 401
    assert "signature_mismatch" in resp.json()["detail"]


def test_warrant_with_mismatched_internal_merchant_id_is_rejected(client):
    """The narrower "confused deputy" case rule 2 (merchant_id_mismatch)
    actually catches: a warrant signed with demo_merchant's OWN secret
    (so it PASSES signature verification against demo_merchant), but
    whose internal merchant_id field claims fit_supply_co -- internally
    inconsistent, and must still be rejected even though the signature
    itself is valid."""
    warrant = {
        "agent_id": "test_agent", "merchant_id": FIT_SUPPLY,  # claims the OTHER merchant
        "per_tx_cap_inr": 2000, "daily_cap_inr": 5000,
        "allowed_categories": ["electronics"], "expires_at": time.time() + 3600,
        "nonce": uuid.uuid4().hex,
    }
    sig = sessions.sign_warrant(warrant, secret=merchant_registry.get_warrant_secret(DEMO))  # but signed with demo's secret

    resp = client.post(f"/merchants/{DEMO}/session/agent", json={"warrant": warrant, "signature": sig})
    assert resp.status_code == 401
    assert "merchant_id_mismatch" in resp.json()["detail"]


def test_warrant_signed_with_wrong_merchants_secret_fails_signature_check(client):
    """Claiming merchant_id=demo_merchant but signing with
    fit_supply_co's secret must fail signature verification -- an
    attacker holding one merchant's secret can't forge a warrant for
    a different merchant just by changing the merchant_id field."""
    warrant = {
        "agent_id": "test_agent", "merchant_id": DEMO,
        "per_tx_cap_inr": 2000, "daily_cap_inr": 5000,
        "allowed_categories": ["electronics"], "expires_at": time.time() + 3600,
        "nonce": uuid.uuid4().hex,
    }
    wrong_secret_sig = sessions.sign_warrant(warrant, secret=merchant_registry.get_warrant_secret(FIT_SUPPLY))

    resp = client.post(f"/merchants/{DEMO}/session/agent", json={"warrant": warrant, "signature": wrong_secret_sig})
    assert resp.status_code == 401
    assert "signature_mismatch" in resp.json()["detail"]


def test_genuinely_signed_fit_supply_warrant_works_at_fit_supply(client):
    resp = sign_and_mint_agent_session(client, FIT_SUPPLY)
    assert resp.status_code == 200
    assert resp.json()["merchant_id"] == FIT_SUPPLY


# ---------------------------------------------------------------------
# Agent rail end to end, at the second merchant
# ---------------------------------------------------------------------

def test_agent_pay_end_to_end_at_fit_supply_co(client):
    session_id = sign_and_mint_agent_session(client, FIT_SUPPLY, per_tx_cap_inr=5000, daily_cap_inr=5000).json()["session_id"]
    add_and_review(client, session_id, "sku_002")  # Whey Protein, Rs.2199

    resp = client.post("/agent/pay", json={"session_id": session_id, "idempotency_key": uuid.uuid4().hex, "confirm": True})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "captured"
    assert body["amount_inr"] == 2199.0


def test_agent_category_restriction_scoped_to_fit_supply_categories(client):
    session_id = sign_and_mint_agent_session(
        client, FIT_SUPPLY, allowed_categories=["equipment"],  # NOT "supplements"
    ).json()["session_id"]
    add_and_review(client, session_id, "sku_002")  # Whey Protein -- category "supplements"

    resp = client.post("/agent/pay", json={"session_id": session_id, "idempotency_key": uuid.uuid4().hex, "confirm": True})
    assert resp.status_code == 403
    assert "category_not_allowed" in resp.json()["detail"]


# ---------------------------------------------------------------------
# Daily cap isolation -- spend at one merchant never affects the other
# ---------------------------------------------------------------------

def test_daily_cap_isolated_between_merchants(client):
    """Capture Rs.1499 worth of spend at demo_merchant, then confirm an
    agent's daily cap at fit_supply_co (a totally separate warrant,
    separate secret) is completely unaffected -- captured spend at one
    merchant must never count against a cap at a different one."""
    demo_session = sign_and_mint_agent_session(client, DEMO, per_tx_cap_inr=2000, daily_cap_inr=2000).json()["session_id"]
    add_and_review(client, demo_session, "sku_001")  # Rs.1499
    resp = client.post("/agent/pay", json={"session_id": demo_session, "idempotency_key": uuid.uuid4().hex, "confirm": True})
    assert resp.status_code == 200
    assert resp.json()["status"] == "captured"

    # fit_supply_co's remaining-cap must show the FULL daily cap still
    # available -- demo_merchant's Rs.1499 spend must not appear here.
    fit_session = sign_and_mint_agent_session(client, FIT_SUPPLY, per_tx_cap_inr=5000, daily_cap_inr=5000).json()["session_id"]
    remaining = client.get("/agent/remaining-cap", params={"session_id": fit_session}).json()
    assert remaining["daily_remaining_inr"] == 5000.0


def test_metrics_and_remaining_cap_after_spend_at_fit_supply_do_not_touch_demo_cap(client):
    """Same isolation, opposite direction -- spend at fit_supply_co must
    not affect demo_merchant's daily cap either."""
    fit_session = sign_and_mint_agent_session(client, FIT_SUPPLY, per_tx_cap_inr=5000, daily_cap_inr=5000).json()["session_id"]
    add_and_review(client, fit_session, "sku_006")  # Yoga Mat, Rs.899
    resp = client.post("/agent/pay", json={"session_id": fit_session, "idempotency_key": uuid.uuid4().hex, "confirm": True})
    assert resp.status_code == 200

    demo_session = sign_and_mint_agent_session(client, DEMO, per_tx_cap_inr=2000, daily_cap_inr=2000).json()["session_id"]
    remaining = client.get("/agent/remaining-cap", params={"session_id": demo_session}).json()
    assert remaining["daily_remaining_inr"] == 2000.0


# ---------------------------------------------------------------------
# Stock isolation
# ---------------------------------------------------------------------

def test_stock_decrement_at_one_merchant_does_not_touch_the_others_sku(client):
    from app import catalog

    demo_stock_before = catalog.get_product(DEMO, "sku_001")["stock"]
    fit_stock_before = catalog.get_product(FIT_SUPPLY, "sku_001")["stock"]

    session_id = mint_human_session(client, FIT_SUPPLY)
    add_and_review(client, session_id, "sku_001")  # fit_supply_co's dumbbells
    checkout_resp = client.post("/checkout", json={"session_id": session_id, "idempotency_key": uuid.uuid4().hex})
    assert checkout_resp.status_code == 200

    # Capture it via the human rail's simulate-capture path.
    from app import webhooks
    import json as json_module
    payment_link_id = None
    for entry in client.get(f"/merchants/{FIT_SUPPLY}/audit-trail", params={"session_id": session_id}).json()["entries"]:
        if entry["action"] == "checkout_payment":
            payment_link_id = json_module.loads(entry["details"])["payment_link_id"]
            break
    body = webhooks.build_capture_payload(payment_link_id=payment_link_id, amount_inr=6499)
    body_bytes = json_module.dumps(body).encode("utf-8")
    sig = webhooks.sign_body(body_bytes)
    cap_resp = client.post("/demo/simulate-capture", content=body_bytes,
                            headers={"X-Razorpay-Signature": sig, "Content-Type": "application/json"})
    assert cap_resp.status_code == 200

    assert catalog.get_product(FIT_SUPPLY, "sku_001")["stock"] == fit_stock_before - 1
    assert catalog.get_product(DEMO, "sku_001")["stock"] == demo_stock_before  # untouched


# ---------------------------------------------------------------------
# Per-merchant metrics
# ---------------------------------------------------------------------

def test_merchant_metrics_endpoint_isolated_from_global(client):
    """A captured purchase at fit_supply_co must show up in
    fit_supply_co's own GET /merchants/{id}/metrics, but not in
    demo_merchant's."""
    session_id = sign_and_mint_agent_session(client, FIT_SUPPLY, per_tx_cap_inr=5000, daily_cap_inr=5000).json()["session_id"]
    add_and_review(client, session_id, "sku_006")  # Yoga Mat, Rs.899
    resp = client.post("/agent/pay", json={"session_id": session_id, "idempotency_key": uuid.uuid4().hex, "confirm": True})
    assert resp.status_code == 200
    assert resp.json()["status"] == "captured"

    fit_metrics = client.get(f"/merchants/{FIT_SUPPLY}/metrics").json()
    assert fit_metrics["merchant_id"] == FIT_SUPPLY
    assert fit_metrics["captured_inr"] == 899.0

    demo_metrics = client.get(f"/merchants/{DEMO}/metrics").json()
    assert demo_metrics["captured_inr"] == 0.0


def test_global_metrics_include_both_merchants_combined(client):
    """GET /metrics (no merchant_id) must still show the SUM across
    every merchant -- unchanged default behavior."""
    demo_session = sign_and_mint_agent_session(client, DEMO, per_tx_cap_inr=2000, daily_cap_inr=2000).json()["session_id"]
    add_and_review(client, demo_session, "sku_001")  # Rs.1499
    r1 = client.post("/agent/pay", json={"session_id": demo_session, "idempotency_key": uuid.uuid4().hex, "confirm": True})
    assert r1.status_code == 200

    fit_session = sign_and_mint_agent_session(client, FIT_SUPPLY, per_tx_cap_inr=5000, daily_cap_inr=5000).json()["session_id"]
    add_and_review(client, fit_session, "sku_006")  # Rs.899
    r2 = client.post("/agent/pay", json={"session_id": fit_session, "idempotency_key": uuid.uuid4().hex, "confirm": True})
    assert r2.status_code == 200

    global_metrics = client.get("/metrics").json()
    assert global_metrics["merchant_id"] is None
    assert global_metrics["captured_inr"] == 1499.0 + 899.0

    # ?merchant_id= query param form works identically to the
    # /merchants/{id}/metrics route.
    scoped_via_query = client.get("/metrics", params={"merchant_id": DEMO}).json()
    scoped_via_route = client.get(f"/merchants/{DEMO}/metrics").json()
    assert scoped_via_query == scoped_via_route
    assert scoped_via_query["captured_inr"] == 1499.0


def test_merchant_metrics_upsell_counts_isolated(client):
    """upsell_shown_count / upsell_accepted_count must also be scoped
    per merchant, not global, when a merchant_id is given."""
    session_id = sign_and_mint_agent_session(client, FIT_SUPPLY, per_tx_cap_inr=8000, daily_cap_inr=8000).json()["session_id"]
    add_resp = add_and_review(client, session_id, "sku_001")  # dumbbells -- upsell -> sku_006 (yoga mat)
    assert add_resp["upsell"]["product_id"] == "sku_006"

    add_and_review(client, session_id, "sku_006")  # accept the upsell

    fit_metrics = client.get(f"/merchants/{FIT_SUPPLY}/metrics").json()
    assert fit_metrics["upsell_shown_count"] >= 1
    assert fit_metrics["upsell_accepted_count"] >= 1

    demo_metrics = client.get(f"/merchants/{DEMO}/metrics").json()
    assert demo_metrics["upsell_shown_count"] == 0
    assert demo_metrics["upsell_accepted_count"] == 0


def test_unknown_merchant_metrics_404s(client):
    resp = client.get("/merchants/not_a_real_merchant/metrics")
    assert resp.status_code == 404
