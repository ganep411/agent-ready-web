"""An independent, read-only Japanese procurement agent using MCP stdio."""
import argparse
import asyncio
import json
import os
import re
import sys
import unicodedata
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from time import perf_counter

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.shared.exceptions import McpError

# Shared units; the agent uses no application or Shopify credentials directly.
from app.specifications import measurement


def parse_request(text):
    parts = [part.strip() for part in re.split(r"[,、]", unicodedata.normalize("NFKC", text).strip())]
    quantity = re.fullmatch(r"([1-9][0-9]{0,2})\s*(?:個|台)\s*(?:を)?\s*(?:見積もって(?:ください)?|見積もり(?:をお願いします)?)[。.]?", parts.pop())
    if not quantity or int(quantity[1]) > 100:
        raise ValueError("数量1〜100と見積依頼を最後の句に指定してください。")
    conditions = {}
    if parts and parts[0].endswith("を"):
        conditions["name"] = parts.pop(0)[:-1].strip()
        if not 1 <= len(conditions["name"]) <= 200:
            raise ValueError("商品名は1〜200文字で指定してください。")
    for part in parts:
        clause = re.fullmatch(r"(型番|SKU|電圧|容量)\s*(?:[:=]|は)?\s*(.+)", part, re.IGNORECASE)
        if not clause:
            raise ValueError("未対応の条件があります。対応形式はREADMEを参照してください。")
        key = {"型番": "mpn", "sku": "sku", "電圧": "voltage", "容量": "capacity"}[clause[1].casefold()]
        value = clause[2].strip()
        if key in ("mpn", "sku"):
            parsed = value if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,127}", value) else None
        else:
            parsed = measurement(value)
            if parsed and ((parsed["unit"] == "V") != (key == "voltage")):
                parsed = None
        if key in conditions or parsed is None:
            raise ValueError("条件の形式、単位、または重複を確認してください。")
        conditions[key] = parsed
    if not conditions:
        raise ValueError("商品名、型番、SKU、電圧、容量のいずれかを指定してください。")
    return conditions, int(quantity[1])


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def compare(item, conditions, quantity):
    checks = {}
    for key, expected in conditions.items():
        if key in ("voltage", "capacity"):
            actual = (item.get("attributes") or {}).get(key)
            matches = actual and all(actual.get(field) == expected[field] for field in ("value", "unit"))
        else:
            actual = item.get("name" if key == "name" else key)
            matches = actual and (expected.casefold() in actual.casefold() if key == "name"
                                  else actual.casefold() == expected.casefold())
        checks[key] = "unknown" if not actual else ("match" if matches else "mismatch")
    stock = item.get("stock")
    checks["stock"] = "unknown" if stock is None else ("match" if stock >= quantity else "insufficient")
    checks["price"] = "unknown" if item.get("price") is None or not item.get("currency") else "known"
    return checks


async def procure(session, text):
    conditions, quantity = parse_request(text)
    metrics = {name: {"calls": 0, "successes": 0, "elapsed_ms": 0.0}
               for name in ("discover", "search", "quote")}

    async def call(name, arguments=None):
        stats = metrics[name]
        stats["calls"] += 1
        start = perf_counter()
        try:
            result = await session.call_tool(name, arguments)
            if result.isError:
                return None
            data = result.structuredContent or json.loads(result.content[0].text)
            if not isinstance(data, dict):
                return None
            stats["successes"] += 1
            return data
        except (McpError, ValueError, IndexError, AttributeError):
            return None
        finally:
            stats["elapsed_ms"] += (perf_counter() - start) * 1000

    output = {"status": "discovery_failed", "quantity": quantity, "candidates": [],
              "reference_quotes": [], "metrics": metrics}
    discovered = await call("discover")
    if discovered:
        output["data_source"] = discovered.get("data_source")
        # A name search also returns candidates whose requested model is unknown.
        query = conditions.get("name") or conditions.get("mpn") or conditions.get("sku", "")
        result = await call("search", {"q": query, "limit": 100, "include_details": True})
        output["status"] = "search_failed"
        if result:
            retrieved_at = utc_now()
            output["retrieved_at"] = retrieved_at
            output["search_total"] = result["total"]
            output["search_truncated"] = result["total"] > len(result["items"])
            for item in result["items"]:
                candidate = {key: item.get(key) for key in
                             ("name", "product_id", "variant_id", "sku", "mpn", "attributes", "price", "currency", "stock")}
                candidate["checks"] = compare(item, conditions, quantity)
                output["candidates"].append(candidate)
            eligible = [item for item in output["candidates"]
                        if all(item["checks"][key] == "match" for key in (*conditions, "stock"))
                        and item["checks"]["price"] == "known" and item["variant_id"]]
            eligible.sort(key=lambda item: (item["currency"], Decimal(item["price"]), item["variant_id"]))
            output["status"] = outcome(output, conditions, eligible)
            # Compare prices within each currency. Bound live quotes to three calls.
            checked_quotes = []
            for item in eligible[:3]:
                quote = await call("quote", {"product_id": item["variant_id"], "quantity": quantity})
                checked_quotes.append(quote)
                if (quote and quote.get("available") is True and quote.get("total_price") is not None
                        and quote.get("currency") and quote.get("binding") is False
                        and quote.get("inventory_reserved") is False):
                    fields = ("quantity", "unit_price", "total_price", "currency", "available", "expires_at",
                              "binding", "inventory_reserved")
                    output["reference_quotes"].append({
                        **{key: quote[key] for key in fields}, "product_id": item["product_id"],
                        "variant_id": item["variant_id"], "retrieved_at": utc_now(),
                    })
            if output["reference_quotes"]:
                output["status"] = "quoted"
            elif checked_quotes and len(checked_quotes) == len(eligible) and not output["search_truncated"]:
                if all(quote and quote.get("available") is False for quote in checked_quotes):
                    output["status"] = "insufficient_stock"
    for stats in metrics.values():
        stats["elapsed_ms"] = round(stats["elapsed_ms"], 2)
        stats["success_rate"] = stats["successes"] / stats["calls"] if stats["calls"] else None
    return output


def outcome(output, conditions, eligible):
    if eligible:
        return "quote_unavailable"
    if output["search_truncated"]:
        return "search_incomplete"
    checks = [item["checks"] for item in output["candidates"]]
    matching = [row for row in checks if all(row[key] == "match" for key in conditions)]
    if matching and all(row["stock"] == "insufficient" for row in matching):
        return "insufficient_stock"
    if any(all(row[key] != "mismatch" for key in conditions) for row in checks):
        return "unknown"
    return "no_match"


async def run(text):
    params = StdioServerParameters(command=sys.executable, args=["-m", "app.mcp_server"],
                                  cwd=str(Path(__file__).resolve().parents[1]), env=dict(os.environ))
    async with asyncio.timeout(120):
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                return await procure(session, text)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Read-only procurement demo over MCP stdio")
    parser.add_argument("request")
    args = parser.parse_args()
    try:
        parse_request(args.request)
    except ValueError as error:
        raise SystemExit(str(error)) from None
    try:
        output = asyncio.run(run(args.request))
    except Exception:
        raise SystemExit("MCP session failed. Check the local server configuration.") from None
    print(json.dumps(output, ensure_ascii=True, indent=2))
