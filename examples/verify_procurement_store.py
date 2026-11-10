"""Run read-only procurement checks against the Shopify development store."""

import argparse
import asyncio
import json
import os
from decimal import Decimal

import httpx

from .procurement_agent import run


def diagnose_metafield(variant_id):
    from app.main import app
    from app.shopify import ShopifyError

    adapter = app.state.catalog
    query = """
    query SpecificationCheck($variant: ID!) {
      productVariant(id: $variant) {
        procurement: metafield(namespace: "procurement", key: "specifications") { type value }
        customProcurement: metafield(namespace: "custom", key: "procurement_specifications") { type value }
        metafields(first: 50) { nodes { namespace key type } }
      }
    }
    """
    try:
        with httpx.Client(timeout=adapter.timeout, follow_redirects=False) as client:
            result = adapter._request(client, query, {"variant": variant_id})
    except ShopifyError as error:
        print("Metafield diagnostic:", error)
        return
    variant = result.get("productVariant") or {}
    primary, alternate = variant.get("procurement"), variant.get("customProcurement")
    print("procurement.specifications:", primary["type"] if primary else "not returned")
    print("custom.procurement_specifications:", alternate["type"] if alternate else "not returned")
    field = primary or alternate
    if field:
        try:
            data = json.loads(field["value"])
        except (ValueError, TypeError):
            data = None
        for key in ("mpn", "capacity"):
            present = isinstance(data, dict) and isinstance(data.get(key), str) and bool(data[key].strip())
            print(f"JSON key {key}:", "present" if present else "missing or invalid")
    else:
        for node in (variant.get("metafields") or {}).get("nodes", []):
            if "spec" in node["key"].lower() or node["namespace"] == "procurement":
                print("Related field:", f'{node["namespace"]}.{node["key"]}', node["type"])


async def verify(with_specifications):
    cases = [
        ("voltage_match", "電圧12 V", "quoted"),
        ("insufficient_stock", "電圧24 V", "insufficient_stock"),
        ("no_match", "電圧36 V", "no_match"),
    ]
    if with_specifications:
        cases.append(("explicit_specifications", "型番ARW-TEST-PS-12、電圧12 V、容量60 W", "quoted"))
    else:
        cases.append(("unknown_specifications", "型番ARW-TEST-PS-12、電圧12 V、容量60 W", "unknown"))

    for label, conditions, expected in cases:
        result = await run(f"DINレール直流電源を、{conditions}、2個見積もって")
        if result.get("data_source") != "ShopifyAdapter":
            raise ValueError("Expected ShopifyAdapter.")
        status = result["status"]
        if status != expected or result.get("search_truncated"):
            if label == "explicit_specifications":
                for item in result["candidates"]:
                    checks = item["checks"]
                    if checks.get("voltage") == "match":
                        print("12 V specification checks:",
                              "mpn=" + checks.get("mpn", "unknown"),
                              "capacity=" + checks.get("capacity", "unknown"))
                        diagnose_metafield(item["variant_id"])
            raise ValueError(f"{label}: expected {expected}, got {status}.")
        quotes = result["reference_quotes"]
        if expected == "quoted":
            if len(quotes) != 1:
                raise ValueError(f"{label}: expected exactly one reference quote.")
            quote = quotes[0]
            if (Decimal(quote["total_price"]) != 8400 or quote["quantity"] != 2
                    or not quote["product_id"] or not quote["variant_id"]
                    or quote["available"] is not True or quote["binding"] or quote["inventory_reserved"]):
                raise ValueError(f"{label}: reference quote did not match.")
        elif quotes or result["metrics"]["quote"]["calls"]:
            raise ValueError(f"{label}: an ineligible candidate was quoted.")
        elif expected == "unknown":
            if not any(item["checks"]["voltage"] == "match"
                       and item["checks"]["mpn"] == "unknown"
                       and item["checks"]["capacity"] == "unknown" for item in result["candidates"]):
                raise ValueError(f"{label}: expected both missing attributes to be unknown.")
        print(f"{label}: {status} (PASS)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--with-specifications", action="store_true")
    args = parser.parse_args()
    if os.getenv("CATALOG_ADAPTER") != "shopify":
        raise SystemExit("Run from the PowerShell configured for the Shopify development store.")
    try:
        asyncio.run(verify(args.with_specifications))
    except ValueError as error:
        raise SystemExit(str(error)) from None
    except Exception:
        raise SystemExit("Shopify MCP session failed.") from None
    print("PASS: Shopify procurement scenarios")
