from __future__ import annotations
"""
The revenue side of the project.

Track 1 asks for an agent that "grows revenue for a merchant". This is that
part. When a buyer is about to purchase something, we suggest one extra item.

The important rule: a suggestion is only allowed if the new total still fits
inside the buyer's spending limit. We never grow revenue by pushing someone
past what they agreed to spend. If the suggestion does not fit, we do not
make it.

How suggestions are chosen: simple rules based on what pairs naturally with
what. Tea goes with an infuser or cups. Coffee goes with a kettle. Gift boxes
do not get add-ons because they are already complete purchases.

This is intentionally not an AI model. A rule table is easy to audit, easy to
explain, and cannot invent a product that does not exist. That last point
matters - a recommendation model that hallucinates a SKU creates a broken
checkout.
"""

from app.catalog import BY_SKU, CATALOG

# Which category of product suggests which add-ons, best first.
PAIRINGS = {
    "tea": ["ACC-002", "ACC-003", "ACC-001"],
    "coffee": ["ACC-001", "ACC-003", "ACC-002"],
    "accessory": ["TEA-003", "COF-001"],
    "gift": [],  # gift boxes are complete on their own
}


def _category(sku: str) -> str:
    tags = BY_SKU[sku]["tags"]
    for cat in ("gift", "tea", "coffee", "accessory"):
        if cat in tags:
            return cat
    return "other"


def suggest(sku: str, current_total_paise: int, limit_paise: int) -> dict | None:
    """
    Suggest one add-on for a purchase, or None if nothing fits.

    Returns the product plus the headroom left, so the caller can show the
    buyer exactly why the suggestion is allowed.
    """
    category = _category(sku)
    headroom = limit_paise - current_total_paise

    if headroom <= 0:
        return None

    for candidate_sku in PAIRINGS.get(category, []):
        candidate = BY_SKU.get(candidate_sku)
        if not candidate:
            continue
        if candidate_sku == sku:
            continue
        if candidate["stock"] <= 0:
            continue
        if candidate["price_paise"] > headroom:
            continue  # would break the spending limit

        return {
            "sku": candidate["sku"],
            "name": candidate["name"],
            "price_paise": candidate["price_paise"],
            "reason": f"pairs with {BY_SKU[sku]['name']}",
            "new_total_paise": current_total_paise + candidate["price_paise"],
            "headroom_left_paise": headroom - candidate["price_paise"],
        }

    return None


def stats() -> dict:
    """Small helper for the README: how many products can be suggested at all."""
    suggestible = {s for skus in PAIRINGS.values() for s in skus}
    return {"catalog_size": len(CATALOG), "suggestible_products": len(suggestible)}
