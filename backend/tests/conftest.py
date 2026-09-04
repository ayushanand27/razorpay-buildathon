"""
Shared pytest fixtures.

Sets deterministic, isolated test config BEFORE the app package is
ever imported (each app module's load_dotenv() call does not override
an already-set os.environ value, so setting these first pins them for
the whole test session regardless of what the real .env contains).

Every test gets a fresh, empty SQLite file for each persisted store
(audit trail, sessions, carts, orders -- see each module's own
DB_PATH) plus fresh catalog stock, via the autouse `_isolated_state`
fixture -- tests never leak state into each other or into the real
demo process's own database files.
"""

import os

os.environ.setdefault("AGENT_WARRANT_SECRET", "test_warrant_secret_do_not_use_in_prod")
os.environ.setdefault("FIT_SUPPLY_WARRANT_SECRET", "test_fit_supply_warrant_secret_do_not_use_in_prod")
os.environ.setdefault("MERCHANT_ID", "demo_merchant")
os.environ["RAZORPAY_KEY_ID"] = ""
os.environ["RAZORPAY_KEY_SECRET"] = ""
os.environ["RAZORPAY_WEBHOOK_SECRET"] = "test_webhook_secret_do_not_use_in_prod"
os.environ["GROQ_API_KEY"] = ""  # keep upsell copy/NLU on the deterministic static-fallback path
# Any GROQ_API_KEY_2, _3, ... backup keys in the real .env must not leak
# into the test session either. This has to happen BEFORE the `app`
# import below (which triggers load_dotenv() transitively) -- dotenv
# never overrides an already-set os.environ value, so pre-blanking a
# generous range here is what actually keeps them out; a loop over
# os.environ run at this point would find nothing yet, since .env
# hasn't been loaded. Tests that specifically want backup keys present
# set them explicitly via monkeypatch.setenv().
for _i in range(2, 10):
    os.environ[f"GROQ_API_KEY_{_i}"] = ""

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
    monkeypatch.setattr(sessions, "DB_PATH", str(tmp_path / "sessions_test.db"))
    monkeypatch.setattr(cart, "DB_PATH", str(tmp_path / "carts_test.db"))
    monkeypatch.setattr(orders, "DB_PATH", str(tmp_path / "orders_test.db"))

    catalog.reset_stock_for_tests()

    yield
