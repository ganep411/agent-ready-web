"""Normalized, explicitly sourced procurement attributes."""
import json
import re
import unicodedata
from decimal import Decimal


def measurement(value):
    if not isinstance(value, str):
        return None
    match = re.fullmatch(r"([0-9]{1,9}(?:\.[0-9]{1,6})?)\s*(mV|V|mAh|Ah|mWh|Wh|W|GB|MB)",
                         unicodedata.normalize("NFKC", value).strip(), re.IGNORECASE)
    if not match:
        return None
    amount, unit = Decimal(match[1]), match[2].casefold()
    units = {"mv": ("V", 1000), "v": ("V", 1), "mah": ("Ah", 1000), "ah": ("Ah", 1),
             "mwh": ("Wh", 1000), "wh": ("Wh", 1), "w": ("W", 1), "gb": ("GB", 1), "mb": ("MB", 1)}
    canonical, divisor = units[unit]
    return {"value": format((amount / divisor).normalize(), "f"), "unit": canonical}


def shopify_details(product, variant):
    field = variant.get("procurement") or variant.get("customProcurement") or {}
    try:
        explicit = json.loads(field.get("value", "")) if field.get("type") == "json" else {}
    except (ValueError, TypeError):
        explicit = {}
    if not isinstance(explicit, dict):
        explicit = {}
    options = {option["name"].casefold(): option.get("value")
               for option in variant.get("selectedOptions") or []
               if isinstance(option, dict) and isinstance(option.get("name"), str)}

    def attribute(key, labels, units):
        value = explicit.get(key)
        source = "variant_metafield"
        if key not in explicit:
            matches = [options[label] for label in labels if label in options]
            value = matches[0] if len(matches) == 1 else None
            source = "variant_option"
        normalized = measurement(value)
        return {**normalized, "source": source} if normalized and normalized["unit"] in units else None

    mpn = explicit.get("mpn")
    return {
        "product_id": product["id"], "variant_id": variant["id"], "sku": variant.get("sku") or None,
        "mpn": mpn.strip() if isinstance(mpn, str) and mpn.strip() else None,
        "attributes": {
            "voltage": attribute("voltage", ("電圧", "出力電圧", "voltage"), {"V"}),
            "capacity": attribute("capacity", ("容量", "定格容量", "capacity"), {"Ah", "Wh", "W", "GB", "MB"}),
        },
    }
