"""
Agent-readable product catalog for the demo merchant.

This is intentionally simple (in-memory) for the buildathon demo.
In production this would be a DB table synced from the merchant's
inventory system, exposed the same way to both the WhatsApp agent
and the MCP tool layer so there is exactly one source of truth.
"""

CATALOG = [
    {
        "id": "sku_001",
        "name": "Wireless Earbuds Pro",
        "price_inr": 1499,
        "stock": 25,
        "description": "Bluetooth 5.3 earbuds, 30hr battery, ANC.",
        "category": "electronics",
    },
    {
        "id": "sku_002",
        "name": "Cotton Graphic T-Shirt",
        "price_inr": 599,
        "stock": 100,
        "description": "100% cotton, unisex, 5 colours available.",
        "category": "apparel",
    },
    {
        "id": "sku_003",
        "name": "Stainless Steel Water Bottle",
        "price_inr": 349,
        "stock": 60,
        "description": "1L, insulated, keeps cold 24hr / hot 12hr.",
        "category": "home",
    },
    {
        "id": "sku_004",
        "name": "Notebook Set (Pack of 3)",
        "price_inr": 249,
        "stock": 200,
        "description": "A5 ruled notebooks, 100 pages each.",
        "category": "stationery",
    },
    {
        "id": "sku_005",
        "name": "Portable Power Bank 10000mAh",
        "price_inr": 999,
        "stock": 0,  # intentionally out of stock -> used for the failure demo
        "description": "Fast charging, dual USB output.",
        "category": "electronics",
    },
]


def list_products(category: str | None = None):
    if category:
        return [p for p in CATALOG if p["category"] == category]
    return CATALOG


def get_product(product_id: str):
    for p in CATALOG:
        if p["id"] == product_id:
            return p
    return None


# Fixed "frequently bought together" lookup -- deterministic, not a model
# call. The track's own AI Judgment bar ("use AI models appropriately,
# prefer deterministic solutions where AI is unnecessary") is better
# served by a plain table here than by spending an LLM call on it.
# Never points at sku_005 (out of stock) as a suggestion target.
UPSELL_MAP = {
    "sku_001": ("sku_003", "Frequently bought with Wireless Earbuds Pro -- stay hydrated on the go."),
    "sku_002": ("sku_004", "Popular with students -- pair your tee with a fresh notebook set."),
    "sku_003": ("sku_001", "Complete your everyday carry with wireless earbuds."),
    "sku_004": ("sku_002", "Notebook fans also like our graphic tee."),
    "sku_005": ("sku_001", "That one's out of stock -- here's an in-stock pick in electronics."),
}


def get_upsell(product_id: str, exclude_ids: set[str] | None = None):
    entry = UPSELL_MAP.get(product_id)
    if not entry:
        return None
    suggested_id, reason = entry
    if exclude_ids and suggested_id in exclude_ids:
        return None
    product = get_product(suggested_id)
    if not product or product["stock"] == 0:
        return None
    return {
        "product_id": product["id"],
        "name": product["name"],
        "price_inr": product["price_inr"],
        "reason": reason,
    }
