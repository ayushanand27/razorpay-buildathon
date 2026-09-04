"""
Single shared SQLite database (app.db) for the whole backend, via
SQLModel (SQLAlchemy + Pydantic) -- replaces the earlier design of one
separate .db file per concern (sessions.db, carts.db, orders.db,
audit_trail.db). Splitting state across files made cross-table
atomicity IMPOSSIBLE: SQLite guarantees ACID within one connection/
transaction, never across separate files. One shared engine, with
proper foreign keys between tables (buyer sessions belong to a
merchant, cart items and orders belong to a session, ...), makes joins
and atomic multi-table writes real SQL operations instead of an
application-level loop resolving ids across files.

get_session() below is the standard unit of work: everything written
inside one `with db.get_session() as s:` block commits together or
not at all. main.py uses this to make "create the order" and "log its
audit-trail entry" one atomic transaction -- a crash between the two
writes can no longer leave one without the other.

Table classes are grouped by domain but all live in this one module on
purpose: this file IS the schema, in one place, so it's obvious what
relates to what without hunting across modules.
"""

import json
import os
import time
from contextlib import contextmanager
from typing import Optional

from sqlalchemy import event
from sqlmodel import Field, Session, SQLModel, create_engine

DB_PATH = os.path.join(os.path.dirname(__file__), "app.db")


def build_engine(db_path: str):
    """Builds a fresh engine bound to db_path, with FK enforcement wired
    up. A plain create_engine() call binds its connection string once
    and for good -- this exists so tests can build an ISOLATED engine
    per test (pointed at a temp file) and swap it in via
    `monkeypatch.setattr(db, "_engine", db.build_engine(tmp_path))`,
    rather than trying to redirect the real app.db's engine after the
    fact."""
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False, "timeout": 5},
    )

    @event.listens_for(engine, "connect")
    def _enable_foreign_keys(dbapi_connection, _record):
        """SQLite has foreign key ENFORCEMENT off by default even when
        the schema declares them -- without this, a foreign_key= on a
        Field is documentation, not a guarantee."""
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    return engine


_engine = build_engine(DB_PATH)


# ---------------------------------------------------------------------
# Merchant registry -- replaces the old hardcoded merchants.py dict.
# Two demo merchants are SEEDED (see seed_default_merchants() below),
# not hardcoded as the live source of truth; POST /merchants (main.py)
# adds a real, persisted merchant at runtime, no source edit needed.
# ---------------------------------------------------------------------

class Merchant(SQLModel, table=True):
    __tablename__ = "merchants"
    merchant_id: str = Field(primary_key=True)
    name: str
    warrant_secret: str
    max_order_inr: float
    created_at: float = Field(default_factory=time.time)


class CatalogProduct(SQLModel, table=True):
    __tablename__ = "catalog_products"
    merchant_id: str = Field(foreign_key="merchants.merchant_id", primary_key=True)
    product_id: str = Field(primary_key=True)
    name: str
    description: str = ""
    price_inr: float = 0
    currency: str = "INR"
    tax_bps: int = 0
    stock: int = 0
    category: str = ""
    attributes_json: str = "{}"
    return_window_days: int = 0


class UpsellMapEntry(SQLModel, table=True):
    __tablename__ = "upsell_map"
    merchant_id: str = Field(foreign_key="merchants.merchant_id", primary_key=True)
    from_product_id: str = Field(primary_key=True)
    to_product_id: str
    static_reason: str


# ---------------------------------------------------------------------
# Buyer sessions -- every session belongs to exactly one merchant.
# ---------------------------------------------------------------------

class BuyerSession(SQLModel, table=True):
    __tablename__ = "sessions"
    session_id: str = Field(primary_key=True)
    merchant_id: str = Field(foreign_key="merchants.merchant_id")
    actor: str
    warrant_json: Optional[str] = None
    warrant_signature: Optional[str] = None
    created_at: float = Field(default_factory=time.time)


class UsedNonce(SQLModel, table=True):
    __tablename__ = "used_nonces"
    nonce: str = Field(primary_key=True)
    created_at: float = Field(default_factory=time.time)


# ---------------------------------------------------------------------
# Carts -- every row also carries merchant_id directly (not just
# derivable via session_id), so a query can filter on merchant_id
# alone without a join, and the boundary is explicit even if a future
# caller queries this table directly.
# ---------------------------------------------------------------------

class CartItem(SQLModel, table=True):
    __tablename__ = "cart_items"
    session_id: str = Field(foreign_key="sessions.session_id", primary_key=True)
    product_id: str = Field(primary_key=True)
    merchant_id: str = Field(foreign_key="merchants.merchant_id")
    name: str
    qty: int
    price_inr: float


class CartReviewed(SQLModel, table=True):
    __tablename__ = "cart_reviewed"
    session_id: str = Field(foreign_key="sessions.session_id", primary_key=True)
    merchant_id: str = Field(foreign_key="merchants.merchant_id")
    reviewed: bool = False


class SuggestedUpsell(SQLModel, table=True):
    __tablename__ = "suggested_upsells"
    session_id: str = Field(foreign_key="sessions.session_id", primary_key=True)
    product_id: str = Field(primary_key=True)
    merchant_id: str = Field(foreign_key="merchants.merchant_id")


class UpsellAcceptance(SQLModel, table=True):
    __tablename__ = "upsell_acceptances"
    id: Optional[int] = Field(default=None, primary_key=True)
    session_id: str = Field(foreign_key="sessions.session_id")
    merchant_id: str = Field(foreign_key="merchants.merchant_id")
    product_id: str
    created_at: float = Field(default_factory=time.time)


# ---------------------------------------------------------------------
# Orders + idempotency.
# ---------------------------------------------------------------------

class Order(SQLModel, table=True):
    __tablename__ = "orders"
    order_id: str = Field(primary_key=True)
    merchant_id: str = Field(foreign_key="merchants.merchant_id")
    session_id: str = Field(foreign_key="sessions.session_id")
    actor: str
    line_items_json: str
    total_inr: float
    idempotency_key: str
    payment_link_id: Optional[str] = None
    razorpay_order_id: Optional[str] = None
    response_json: str
    status: str = "created"
    payment_id: Optional[str] = None
    created_at: float = Field(default_factory=time.time)


class IdempotencyRecord(SQLModel, table=True):
    """order_id is deliberately NOT a foreign key here: claim_idempotency_key()
    below inserts this row to atomically RESERVE the (session_id,
    idempotency_key) pair BEFORE the Order row it points at exists --
    the Order is created immediately afterward, in the same logical
    operation, but a strict FK would reject the reservation insert
    itself. The real integrity guarantee this table provides is its
    PRIMARY KEY: SQLite enforces that uniquely and atomically even
    across separate processes (not just threads in one process), which
    is what actually closes the idempotency race under a
    multi-worker deployment -- the in-memory lock in main.py only ever
    helped within a single process."""
    __tablename__ = "idempotency"
    session_id: str = Field(foreign_key="sessions.session_id", primary_key=True)
    idempotency_key: str = Field(primary_key=True)
    order_id: str


# ---------------------------------------------------------------------
# Audit log -- merchant_id is a REAL column now (not resolved via a
# per-row session lookup at query time). Nullable ONLY for the rare
# webhook-meta entries logged before any order/session is matched
# (e.g. an unrecognized signature, or a webhook for a payment_link_id
# this backend never created) -- every buyer-initiated action always
# has a resolvable merchant_id and is stored with one.
# ---------------------------------------------------------------------

class AuditLog(SQLModel, table=True):
    __tablename__ = "audit_log"
    id: Optional[int] = Field(default=None, primary_key=True)
    merchant_id: Optional[str] = Field(default=None, foreign_key="merchants.merchant_id")
    timestamp: float = Field(default_factory=time.time)
    actor: str
    actor_id: Optional[str] = None
    action: str
    amount_inr: Optional[float] = None
    status: str
    details: str = "{}"


def create_db_and_tables():
    SQLModel.metadata.create_all(_engine)


# ---------------------------------------------------------------------
# Seed data -- the two demo merchants' STARTING catalogs, inserted only
# if the merchants table is empty. This is initial data, not the live
# registry: once seeded, a merchant's catalog is read from and written
# to catalog_products like any other merchant's, including one added
# later via POST /merchants (which starts with an empty catalog and
# whatever products are POSTed to it, unrelated to this seed).
# ---------------------------------------------------------------------

_SEED_MERCHANTS = [
    {
        "merchant_id": "demo_merchant",
        "name": "Demo Merchant Store",
        "warrant_secret_env": "AGENT_WARRANT_SECRET",
        "max_order_inr": 10_000,
        "catalog": [
            {"product_id": "sku_001", "name": "Wireless Earbuds Pro",
             "description": "Bluetooth 5.3 earbuds, 30hr battery, ANC.",
             "price_inr": 1499, "currency": "INR", "tax_bps": 1800, "stock": 25,
             "category": "electronics",
             "attributes": {"connectivity": "Bluetooth 5.3", "battery_hours": 30, "noise_cancelling": True},
             "return_window_days": 7},
            {"product_id": "sku_002", "name": "Cotton Graphic T-Shirt",
             "description": "100% cotton, unisex, 5 colours available.",
             "price_inr": 599, "currency": "INR", "tax_bps": 1200, "stock": 100,
             "category": "apparel",
             "attributes": {"material": "100% cotton", "colours": 5, "fit": "unisex"},
             "return_window_days": 15},
            {"product_id": "sku_003", "name": "Stainless Steel Water Bottle",
             "description": "1L, insulated, keeps cold 24hr / hot 12hr.",
             "price_inr": 349, "currency": "INR", "tax_bps": 1800, "stock": 60,
             "category": "home",
             "attributes": {"capacity_litres": 1, "insulated": True},
             "return_window_days": 7},
            {"product_id": "sku_004", "name": "Notebook Set (Pack of 3)",
             "description": "A5 ruled notebooks, 100 pages each.",
             "price_inr": 249, "currency": "INR", "tax_bps": 1200, "stock": 200,
             "category": "stationery",
             "attributes": {"pack_size": 3, "pages_each": 100, "size": "A5"},
             "return_window_days": 15},
            {"product_id": "sku_005", "name": "Portable Power Bank 10000mAh",
             "description": "Fast charging, dual USB output.",
             "price_inr": 999, "currency": "INR", "tax_bps": 1800, "stock": 0,
             "category": "electronics",
             "attributes": {"capacity_mah": 10000, "usb_ports": 2},
             "return_window_days": 7},
        ],
        "upsell_map": {
            "sku_001": ("sku_003", "Frequently bought with Wireless Earbuds Pro -- stay hydrated on the go."),
            "sku_002": ("sku_004", "Popular with students -- pair your tee with a fresh notebook set."),
            "sku_003": ("sku_001", "Complete your everyday carry with wireless earbuds."),
            "sku_004": ("sku_002", "Notebook fans also like our graphic tee."),
            "sku_005": ("sku_001", "That one's out of stock -- here's an in-stock pick in electronics."),
        },
    },
    {
        "merchant_id": "fit_supply_co",
        "name": "FitSupply Co.",
        "warrant_secret_env": "FIT_SUPPLY_WARRANT_SECRET",
        "max_order_inr": 15_000,
        # Deliberately reuses "sku_001"/"sku_002" -- same string, totally
        # different products, to prove per-merchant scoping is real.
        "catalog": [
            {"product_id": "sku_001", "name": "Adjustable Dumbbell Set (5-25kg)",
             "description": "Pair of quick-adjust dumbbells, 5kg to 25kg per side.",
             "price_inr": 6499, "currency": "INR", "tax_bps": 1800, "stock": 12,
             "category": "equipment", "attributes": {"weight_range_kg": "5-25", "pair": True},
             "return_window_days": 10},
            {"product_id": "sku_002", "name": "Whey Protein 1kg -- Chocolate",
             "description": "24g protein per serving, 30 servings per tub.",
             "price_inr": 2199, "currency": "INR", "tax_bps": 500, "stock": 40,
             "category": "supplements",
             "attributes": {"flavour": "chocolate", "protein_g_per_serving": 24, "servings": 30},
             "return_window_days": 0},
            {"product_id": "sku_006", "name": "Yoga Mat -- Non-Slip 6mm",
             "description": "6mm thick, non-slip both sides, carry strap included.",
             "price_inr": 899, "currency": "INR", "tax_bps": 1200, "stock": 55,
             "category": "equipment", "attributes": {"thickness_mm": 6, "carry_strap": True},
             "return_window_days": 10},
            {"product_id": "sku_007", "name": "Resistance Band Set (5 levels)",
             "description": "5 bands, light to extra-heavy, door anchor included.",
             "price_inr": 799, "currency": "INR", "tax_bps": 1200, "stock": 70,
             "category": "equipment", "attributes": {"levels": 5, "door_anchor": True},
             "return_window_days": 10},
            {"product_id": "sku_008", "name": "Pre-Workout 300g -- Fruit Punch",
             "description": "Caffeine + beta-alanine, 30 servings.",
             "price_inr": 1699, "currency": "INR", "tax_bps": 500, "stock": 0,
             "category": "supplements", "attributes": {"flavour": "fruit punch", "servings": 30},
             "return_window_days": 0},
        ],
        "upsell_map": {
            "sku_001": ("sku_006", "Pair your dumbbells with a mat for floor work."),
            "sku_002": ("sku_008", "Stack your whey with a pre-workout for training days."),
            "sku_006": ("sku_007", "Bands travel well alongside a mat for full-body sessions."),
            "sku_007": ("sku_006", "A mat rounds out a resistance-band home setup nicely."),
            "sku_008": ("sku_002", "That one's out of stock -- our whey protein is in stock and pairs well."),
        },
    },
]


def seed_default_merchants():
    """Inserts the two demo merchants and their starting catalogs ONLY
    if they don't already exist -- safe to call on every startup.
    warrant_secret is read from the merchant's own env var at seed
    time (same REQUIRED, no-blank-fallback rule as before); a merchant
    created later via POST /merchants supplies its own secret directly
    instead, no env var involved."""
    with get_session() as s:
        for merchant_def in _SEED_MERCHANTS:
            existing = s.get(Merchant, merchant_def["merchant_id"])
            if existing:
                continue
            secret = os.environ.get(merchant_def["warrant_secret_env"], "")
            s.add(Merchant(
                merchant_id=merchant_def["merchant_id"],
                name=merchant_def["name"],
                warrant_secret=secret,
                max_order_inr=merchant_def["max_order_inr"],
            ))
            s.flush()  # Merchant must exist (FK target) before its catalog rows are inserted
            for p in merchant_def["catalog"]:
                s.add(CatalogProduct(
                    merchant_id=merchant_def["merchant_id"],
                    product_id=p["product_id"], name=p["name"], description=p["description"],
                    price_inr=p["price_inr"], currency=p["currency"], tax_bps=p["tax_bps"],
                    stock=p["stock"], category=p["category"],
                    attributes_json=json_dumps(p["attributes"]),
                    return_window_days=p["return_window_days"],
                ))
            for from_id, (to_id, reason) in merchant_def["upsell_map"].items():
                s.add(UpsellMapEntry(
                    merchant_id=merchant_def["merchant_id"],
                    from_product_id=from_id, to_product_id=to_id, static_reason=reason,
                ))


_SEED_STOCK = {
    (m["merchant_id"], p["product_id"]): p["stock"]
    for m in _SEED_MERCHANTS for p in m["catalog"]
}


def reset_seed_stock_for_tests(merchant_id: str | None = None):
    """Test-only -- restores the two SEED merchants' stock to its
    original catalog value between pytest test functions. Only applies
    to seed data; a merchant created at runtime via POST /merchants has
    no seed values to reset to and manages its own stock explicitly."""
    with get_session() as s:
        for (mid, pid), stock in _SEED_STOCK.items():
            if merchant_id is not None and mid != merchant_id:
                continue
            product = s.get(CatalogProduct, (mid, pid))
            if product:
                product.stock = stock
                s.add(product)


@contextmanager
def get_session():
    """The standard unit of work: everything added/updated inside one
    `with db.get_session() as s:` block commits together, or (on any
    exception) rolls back together. Two writes that must be atomic
    (e.g. an order and its audit-trail entry) just need to happen
    inside the SAME `with` block -- no separate transaction plumbing
    required."""
    create_db_and_tables()
    # expire_on_commit=False: callers throughout this codebase routinely
    # build their return dict from ORM objects AFTER the `with
    # db.get_session()` block has exited (e.g. cart.get_cart()'s list
    # comprehension). SQLAlchemy's default (expire_on_commit=True) marks
    # every attribute on those objects as needing a fresh DB read the
    # instant commit() runs, but the session is closed by then --
    # DetachedInstanceError. Since every unit of work here is a single
    # request-scoped `with` block, not a long-lived session mutated from
    # multiple places, disabling that auto-expiry is safe and matches
    # how this module is actually used everywhere.
    session = Session(_engine, expire_on_commit=False)
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def claim_idempotency_key(session_id: str, idempotency_key: str, order_id: str) -> bool:
    """Atomically claims (session_id, idempotency_key) for order_id via
    a real database PRIMARY KEY constraint -- returns True if THIS
    caller now owns the claim (proceed to create the order), False if
    a DIFFERENT request already claimed it first. Unlike an in-memory
    lock, this holds even across multiple worker processes: two
    processes racing the same key will have exactly one INSERT
    succeed, because SQLite itself rejects the second one."""
    from sqlalchemy.exc import IntegrityError

    try:
        with get_session() as s:
            s.add(IdempotencyRecord(session_id=session_id, idempotency_key=idempotency_key, order_id=order_id))
        return True
    except IntegrityError:
        return False


def release_idempotency_key(session_id: str, idempotency_key: str):
    """Releases a claim that never went on to create a real Order (the
    guardrail/policy check blocked the attempt, or the payment call
    itself failed) so the SAME (session_id, idempotency_key) can be
    retried -- only a claim that actually produced an Order should ever
    be permanent. No-op if the claim doesn't exist (already released,
    or never claimed)."""
    with get_session() as s:
        record = s.get(IdempotencyRecord, (session_id, idempotency_key))
        if record:
            s.delete(record)


def find_order_for_idempotency_key(session_id: str, idempotency_key: str,
                                    wait_seconds: float = 2.0) -> Optional["Order"]:
    """Looks up the order a (session_id, idempotency_key) claim points
    at. If the claim exists but the order isn't visible yet (a narrow
    window: a DIFFERENT, concurrent request just won the claim and is
    still creating its order -- e.g. the Razorpay API call is still in
    flight), retries briefly rather than reporting cart_empty/not_found
    for what is genuinely a duplicate of an in-progress request."""
    deadline = time.time() + wait_seconds
    while True:
        with get_session() as s:
            record = s.get(IdempotencyRecord, (session_id, idempotency_key))
            if record:
                order = s.get(Order, record.order_id)
                if order:
                    s.expunge(order)
                    return order
        if time.time() >= deadline:
            return None
        time.sleep(0.05)


def json_loads(s: str | None) -> dict:
    return json.loads(s) if s else {}


def json_dumps(d: dict | None) -> str:
    return json.dumps(d or {})
