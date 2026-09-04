"""
Shared pytest fixtures.

Sets deterministic, isolated test config BEFORE the app package is
ever imported (each app module's load_dotenv() call does not override
an already-set os.environ value, so setting these first pins them for
the whole test session regardless of what the real .env contains).

Every test gets a fresh, empty SQLite file for the ENTIRE shared
database (app.db's merchants/sessions/carts/orders/audit_log tables
all live together now -- see db.py) via the autouse `_isolated_state`
fixture below, seeded with the two demo merchants fresh each time --
tests never leak state into each other or into the real demo
process's own app.db.

A plain `create_engine()` binds its connection string once and for
good, so the OLD pattern of monkeypatching each module's DB_PATH
string no longer isolates anything for SQLAlchemy -- db.build_engine()
exists specifically so a test can swap in a genuinely fresh, isolated
engine (see db.py's docstring on why).
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

from app import db
from app.main import app


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "_engine", db.build_engine(str(tmp_path / "app_test.db")))
    db.create_db_and_tables()
    db.seed_default_merchants()
    yield
