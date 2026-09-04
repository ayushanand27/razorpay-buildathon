"""
Merchant registry -- reads and writes db.Merchant, a real database
table, not a hardcoded Python dict. Two merchants are SEEDED at
startup (db.seed_default_merchants(), inserted only if not already
present) purely as initial data; a third merchant is provisioned at
RUNTIME via create_merchant() (POST /merchants in main.py) -- no
source edit, no redeploy, no PR required. Nothing in this module (or
anywhere else in the codebase) hardcodes which merchants exist.
"""

import secrets
import time

from sqlalchemy.exc import IntegrityError

from . import db


def list_merchants() -> list[dict]:
    from sqlmodel import select
    with db.get_session() as s:
        rows = s.exec(select(db.Merchant)).all()
        return [{"merchant_id": m.merchant_id, "name": m.name} for m in rows]


def get_merchant(merchant_id: str) -> dict | None:
    with db.get_session() as s:
        m = s.get(db.Merchant, merchant_id)
        if not m:
            return None
        return {"merchant_id": m.merchant_id, "name": m.name,
                "warrant_secret": m.warrant_secret, "max_order_inr": m.max_order_inr}


def get_warrant_secret(merchant_id: str) -> str:
    merchant = get_merchant(merchant_id)
    return merchant["warrant_secret"] if merchant else ""


def get_max_order_inr(merchant_id: str) -> float:
    merchant = get_merchant(merchant_id)
    return merchant["max_order_inr"] if merchant else 0.0


class MerchantAlreadyExists(Exception):
    pass


def create_merchant(merchant_id: str, name: str, max_order_inr: float,
                     warrant_secret: str | None = None) -> dict:
    """Provisions a new merchant AT RUNTIME -- no code change or
    redeploy needed for a real sales team to onboard a third merchant.
    If warrant_secret is omitted, a fresh one is generated server-side
    and returned in the response -- exactly like a real API-key
    issuance flow, and the ONLY time it's shown in the clear (the
    database stores it, but no endpoint ever echoes an existing
    merchant's secret back out). Starts with an EMPTY catalog; add
    products separately (POST /merchants/{merchant_id}/catalog,
    if/when that's needed) -- this function only creates the tenant
    itself. Raises MerchantAlreadyExists if merchant_id is taken."""
    secret = warrant_secret or secrets.token_hex(24)
    try:
        with db.get_session() as s:
            s.add(db.Merchant(
                merchant_id=merchant_id, name=name,
                warrant_secret=secret, max_order_inr=max_order_inr,
                created_at=time.time(),
            ))
    except IntegrityError:
        raise MerchantAlreadyExists(merchant_id)
    return {"merchant_id": merchant_id, "name": name, "max_order_inr": max_order_inr,
            "warrant_secret": secret}
