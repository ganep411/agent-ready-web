"""Read-only checks for the development store after importing the test CSV."""

import asyncio
import csv
import json
import os
import sys
from decimal import Decimal
from pathlib import Path

import httpx
from fastapi.testclient import TestClient
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


def report_publication(adapter, item):
    query = """
    query PublicationCheck($variant: ID!, $channel: ID!) {
      publication(id: $channel) { name }
      productVariant(id: $variant) {
        product { handle status publishedOnPublication(publicationId: $channel) }
      }
    }
    """
    with httpx.Client(timeout=adapter.timeout, follow_redirects=False) as client:
        result = adapter._request(client, query, {"variant": item["id"], "channel": adapter.publication_id})
    print("Configured channel:", (result["publication"] or {}).get("name"))
    print("Unexpected product:", result["productVariant"]["product"])


async def verify_mcp(variants, hidden):
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "app.mcp_server"], env=dict(os.environ),
        cwd=str(Path(__file__).resolve().parents[1]),
    )
    async with asyncio.timeout(120):
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()

                async def call(name, arguments=None):
                    result = await session.call_tool(name, arguments)
                    if result.isError:
                        raise RuntimeError(f"MCP {name} failed.")
                    return result.structuredContent or json.loads(result.content[0].text)

                if (await call("discover"))["data_source"] != "ShopifyAdapter":
                    return False
                ok = True
                for item, rest_quote in variants:
                    result = await call("search", {"q": item["name"]})
                    match = next((entry for entry in result["items"] if entry["id"] == item["id"]), None)
                    quote = await call("quote", {"product_id": item["id"], "quantity": 2})
                    fields = ("product_id", "quantity", "unit_price", "total_price", "currency",
                              "available", "binding", "inventory_reserved")
                    matched = match == item and all(quote[key] == rest_quote[key] for key in fields)
                    print("MCP search and quote:", item["name"], "PASS" if matched else "FAIL")
                    ok &= matched
                for title in sorted(hidden):
                    excluded = (await call("search", {"q": title}))["total"] == 0
                    print("MCP publication:", title, "PASS" if excluded else "FAIL")
                    ok &= excluded
                return ok


def main():
    if os.getenv("CATALOG_ADAPTER") != "shopify":
        raise SystemExit("Set CATALOG_ADAPTER=shopify in the configured PowerShell.")

    from app.main import app

    source = Path(__file__).resolve().parents[2] / "shopify_test_products.csv"
    visible, hidden = set(), set()
    with source.open(encoding="utf-8-sig", newline="") as stream:
        for row in csv.DictReader(stream):
            if row["Title"]:
                title = row["Title"]
                published = (row["Status"] == "active"
                             and row["Published on online store"].lower() == "true")
                if not published:
                    hidden.add(title)
            if published:
                option = row["Option1 value"]
                visible.add(title if option == "Default Title" else f"{title} / {option}")

    with TestClient(app, headers={"Authorization": "Bearer " + os.environ["AGENT_API_KEY"]}) as client:
        discover = client.get("/discover")
        catalog = client.get("/search", params={"limit": 100})
        if discover.status_code != 200 or catalog.status_code != 200:
            raise SystemExit(f"REST HTTP status: discover={discover.status_code}, search={catalog.status_code}")
        print("REST discover:", discover.json()["data_source"])
        result = catalog.json()
        items = {item["name"]: item for item in result["items"]}
        print("Published variants:", result["total"])
        print("Extra catalog names:", sorted(items.keys() - visible))
        missing = visible - items.keys()
        print("Missing CSV names:", sorted(missing))
        leaked = []
        for title in sorted(hidden):
            response = client.get("/search", params={"q": title})
            if response.status_code != 200:
                raise SystemExit(f"Publication check failed (HTTP {response.status_code})")
            matches = response.json()["items"]
            if matches:
                leaked.append(title)
                for item in matches:
                    report_publication(app.state.catalog, item)
        print("Hidden titles returned:", leaked)
        ok = not (missing or leaked)
        variants = []
        for voltage, stock, price in (("12 V", 18, 4200), ("24 V", 0, 4600)):
            item = items.get(f"DINレール直流電源 / {voltage}")
            if not item:
                ok = False
                continue
            response = client.post("/quote", json={"product_id": item["id"], "quantity": 2})
            if response.status_code != 200:
                raise SystemExit(f"Quote failed for {voltage} (HTTP {response.status_code})")
            quote = response.json()
            variants.append((item, quote))
            print(voltage, "stock:", item["stock"], "total:", quote["total_price"],
                  quote["currency"], "available:", quote["available"])
            ok &= (item["stock"] == stock and quote["available"] is (stock >= 2)
                   and quote["total_price"] is not None and Decimal(quote["total_price"]) == price * 2
                   and bool(quote["currency"]) and quote["currency"] == item["currency"]
                   and not quote["binding"] and not quote["inventory_reserved"])
    if not ok:
        raise SystemExit("Some expected store values did not match.")
    if not asyncio.run(verify_mcp(variants, hidden)):
        raise SystemExit("MCP results differed from REST or returned a hidden product.")
    print("PASS: Shopify REST and MCP checks")


if __name__ == "__main__":
    main()
