"""
Merchant registry -- multi-tenant support.

Each merchant has its own catalog and its own warrant secret (read
from its own env var, not a single shared one) -- an agent authorized
against one merchant's secret can never mint a session against a
different merchant, even if it somehow learned that merchant's id.
Deliberately reuses SKU ids ("sku_001") ACROSS merchants below to prove
real isolation: catalog.py always scopes lookups by (merchant_id,
product_id) together, so the two merchants' "sku_001" never collide.

Static catalog definitions (id, name, price, category, ...) live here;
catalog.py owns the MUTABLE runtime state (current stock) seeded from
these at import time, plus all query/upsell logic -- the same split
the single-merchant version already had, now indexed by merchant_id
too.

New merchants are added by appending to MERCHANTS below and setting
their warrant_secret_env in .env -- nothing else in the codebase
hardcodes "demo_merchant".
"""

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

MERCHANTS = {
    "demo_merchant": {
        "merchant_id": "demo_merchant",
        "name": "Demo Merchant Store",
        "warrant_secret_env": "AGENT_WARRANT_SECRET",
        "max_order_inr": 10_000,
        "catalog": [
            {
                "id": "sku_001", "sku": "sku_001", "name": "Wireless Earbuds Pro",
                "description": "Bluetooth 5.3 earbuds, 30hr battery, ANC.",
                "price_inr": 1499, "currency": "INR", "tax_bps": 1800, "stock": 25,
                "category": "electronics",
                "attributes": {"connectivity": "Bluetooth 5.3", "battery_hours": 30, "noise_cancelling": True},
                "return_window_days": 7,
            },
            {
                "id": "sku_002", "sku": "sku_002", "name": "Cotton Graphic T-Shirt",
                "description": "100% cotton, unisex, 5 colours available.",
                "price_inr": 599, "currency": "INR", "tax_bps": 1200, "stock": 100,
                "category": "apparel",
                "attributes": {"material": "100% cotton", "colours": 5, "fit": "unisex"},
                "return_window_days": 15,
            },
            {
                "id": "sku_003", "sku": "sku_003", "name": "Stainless Steel Water Bottle",
                "description": "1L, insulated, keeps cold 24hr / hot 12hr.",
                "price_inr": 349, "currency": "INR", "tax_bps": 1800, "stock": 60,
                "category": "home",
                "attributes": {"capacity_litres": 1, "insulated": True},
                "return_window_days": 7,
            },
            {
                "id": "sku_004", "sku": "sku_004", "name": "Notebook Set (Pack of 3)",
                "description": "A5 ruled notebooks, 100 pages each.",
                "price_inr": 249, "currency": "INR", "tax_bps": 1200, "stock": 200,
                "category": "stationery",
                "attributes": {"pack_size": 3, "pages_each": 100, "size": "A5"},
                "return_window_days": 15,
            },
            {
                "id": "sku_005", "sku": "sku_005", "name": "Portable Power Bank 10000mAh",
                "description": "Fast charging, dual USB output.",
                "price_inr": 999, "currency": "INR", "tax_bps": 1800,
                "stock": 0,  # intentionally out of stock -> used for the failure demo
                "category": "electronics",
                "attributes": {"capacity_mah": 10000, "usb_ports": 2},
                "return_window_days": 7,
            },
        ],
        # (from_product_id -> (to_product_id, static_reason)) -- see
        # catalog.get_upsell(). Never points at an OOS SKU as a target.
        "upsell_map": {
            "sku_001": ("sku_003", "Frequently bought with Wireless Earbuds Pro -- stay hydrated on the go."),
            "sku_002": ("sku_004", "Popular with students -- pair your tee with a fresh notebook set."),
            "sku_003": ("sku_001", "Complete your everyday carry with wireless earbuds."),
            "sku_004": ("sku_002", "Notebook fans also like our graphic tee."),
            "sku_005": ("sku_001", "That one's out of stock -- here's an in-stock pick in electronics."),
        },
    },
    "fit_supply_co": {
        "merchant_id": "fit_supply_co",
        "name": "FitSupply Co.",
        "warrant_secret_env": "FIT_SUPPLY_WARRANT_SECRET",
        "max_order_inr": 15_000,
        # Deliberately reuses "sku_001"/"sku_002" as ids -- same string,
        # completely different products, to prove per-merchant scoping.
        "catalog": [
            {
                "id": "sku_001", "sku": "sku_001", "name": "Adjustable Dumbbell Set (5-25kg)",
                "description": "Pair of quick-adjust dumbbells, 5kg to 25kg per side.",
                "price_inr": 6499, "currency": "INR", "tax_bps": 1800, "stock": 12,
                "category": "equipment",
                "attributes": {"weight_range_kg": "5-25", "pair": True},
                "return_window_days": 10,
            },
            {
                "id": "sku_002", "sku": "sku_002", "name": "Whey Protein 1kg -- Chocolate",
                "description": "24g protein per serving, 30 servings per tub.",
                "price_inr": 2199, "currency": "INR", "tax_bps": 500, "stock": 40,
                "category": "supplements",
                "attributes": {"flavour": "chocolate", "protein_g_per_serving": 24, "servings": 30},
                "return_window_days": 0,  # consumable -- no returns, unlike demo_merchant's defaults
            },
            {
                "id": "sku_006", "sku": "sku_006", "name": "Yoga Mat -- Non-Slip 6mm",
                "description": "6mm thick, non-slip both sides, carry strap included.",
                "price_inr": 899, "currency": "INR", "tax_bps": 1200, "stock": 55,
                "category": "equipment",
                "attributes": {"thickness_mm": 6, "carry_strap": True},
                "return_window_days": 10,
            },
            {
                "id": "sku_007", "sku": "sku_007", "name": "Resistance Band Set (5 levels)",
                "description": "5 bands, light to extra-heavy, door anchor included.",
                "price_inr": 799, "currency": "INR", "tax_bps": 1200, "stock": 70,
                "category": "equipment",
                "attributes": {"levels": 5, "door_anchor": True},
                "return_window_days": 10,
            },
            {
                "id": "sku_008", "sku": "sku_008", "name": "Pre-Workout 300g -- Fruit Punch",
                "description": "Caffeine + beta-alanine, 30 servings.",
                "price_inr": 1699, "currency": "INR", "tax_bps": 500,
                "stock": 0,  # intentionally out of stock -> mirrors demo_merchant's failure-demo SKU
                "category": "supplements",
                "attributes": {"flavour": "fruit punch", "servings": 30},
                "return_window_days": 0,
            },
        ],
        "upsell_map": {
            "sku_001": ("sku_006", "Pair your dumbbells with a mat for floor work."),
            "sku_002": ("sku_008", "Stack your whey with a pre-workout for training days."),
            "sku_006": ("sku_007", "Bands travel well alongside a mat for full-body sessions."),
            "sku_007": ("sku_006", "A mat rounds out a resistance-band home setup nicely."),
            "sku_008": ("sku_002", "That one's out of stock -- our whey protein is in stock and pairs well."),
        },
    },
}


def list_merchants() -> list[dict]:
    return [{"merchant_id": m["merchant_id"], "name": m["name"]} for m in MERCHANTS.values()]


def get_merchant(merchant_id: str) -> dict | None:
    return MERCHANTS.get(merchant_id)


def get_warrant_secret(merchant_id: str) -> str:
    merchant = MERCHANTS.get(merchant_id)
    if not merchant:
        return ""
    return os.environ.get(merchant["warrant_secret_env"], "")


def get_max_order_inr(merchant_id: str) -> float:
    merchant = MERCHANTS.get(merchant_id)
    return merchant["max_order_inr"] if merchant else 0.0
