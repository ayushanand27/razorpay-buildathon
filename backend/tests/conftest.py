"""
Shared pytest fixtures.

Sets deterministic, isolated test config BEFORE the app package is
ever imported (each app module's load_dotenv() call does not override
an already-set os.environ value, so setting these first pins them for
the whole test session regardless of what the real .env contains).

Every test gets a fresh audit DB (a temp file, not the real
audit_trail.db) and fresh in-memory state (carts, sessions, orders,
catalog stock) via the autouse `_isolated_state` fixture -- tests never
leak state into each other or into the real demo process (which is a
separate Python process anyway, but within a single `pytest` run the
same imported modules are shared across all test functions).
"""

import os

os.environ.setdefault("AGENT_WARRANT_SECRET", "test_warrant_secret_do_not_use_in_prod")
os.environ.setdefault("MERCHANT_ID", "demo_merchant")
os.environ["RAZORPAY_KEY_ID"] = ""
os.environ["RAZORPAY_KEY_SECRET"] = ""
os.environ["RAZORPAY_WEBHOOK_SECRET"] = "test_webhook_secret_do_not_use_in_prod"
os.environ["GROQ_API_KEY"] = ""  # keep upsell copy on the deterministic static-fallback path

import pytest
from fastapi.testclient import TestClient

from app import audit, cart, catalog, orders, sessions
from app.main import app


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    monkeypatch.setattr(audit, "DB_PATH", str(tmp_path / "audit_trail_test.db"))

    cart._CARTS.clear()
    cart._CART_REVIEWED.clear()
    cart._SUGGESTED_UPSELLS.clear()
    cart._upsell_accepted_count = 0

    sessions._SESSIONS.clear()
    sessions._USED_NONCES.clear()

    orders._ORDERS.clear()
    orders._IDEMPOTENCY.clear()

    catalog.reset_stock_for_tests()

    yield
