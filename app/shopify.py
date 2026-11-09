"""Merchant-authorized, read-only Admin GraphQL catalog integration."""
import re
import math
import time
from decimal import Decimal, InvalidOperation

import httpx


class ShopifyError(RuntimeError):
    pass


class ShopifyAdapter:
    def __init__(self, shop, token, api_version="2026-10", transport=None,
                 publication_id=None, timeout=12, max_retries=3, sleep=time.sleep):
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]*\.myshopify\.com", shop):
            raise ValueError("SHOPIFY_SHOP must be a *.myshopify.com hostname")
        if not re.fullmatch(r"20\d{2}-(01|04|07|10)", api_version):
            raise ValueError("Invalid Shopify API version")
        if not token or any(c in token for c in '\r\n'):
            raise ValueError("Invalid Shopify token")
        if publication_id is not None and not re.fullmatch(r"gid://shopify/Publication/\d+", publication_id):
            raise ValueError("Invalid publication ID")
        if not 0 <= max_retries <= 5 or timeout <= 0:
            raise ValueError("Invalid request limits")
        self.url = f"https://{shop}/admin/api/{api_version}/graphql.json"
        self._token = token
        self.publication_id = publication_id
        self.transport, self.timeout = transport, timeout
        self.max_retries, self.sleep = max_retries, sleep

    def _request(self, client, query, variables):
        for attempt in range(self.max_retries + 1):
            delay = min(2 ** attempt, 10)
            retry = False
            try:
                response = client.post(self.url, headers={"X-Shopify-Access-Token": self._token},
                                       json={"query": query, "variables": variables})
                retry = response.status_code == 429 or response.status_code in (500, 502, 503, 504)
                if retry:
                    try:
                        retry_after = float(response.headers.get("Retry-After", delay))
                        if math.isfinite(retry_after):
                            delay = min(max(retry_after, 0), 60)
                    except ValueError:
                        pass
                else:
                    response.raise_for_status()
                    payload = response.json()
                    errors = payload.get("errors", [])
                    retry = bool(errors) and all(e.get("extensions", {}).get("code") == "THROTTLED" for e in errors)
                    if errors and not retry:
                        # A field-level price/inventory error may leave a usable catalog.
                        # Explicitly erase the affected value; never trust partial money/stock.
                        partial = payload.get("data")
                        for error in errors:
                            path = error.get("path")
                            if not isinstance(partial, dict) or not isinstance(path, list) or not path or path[-1] not in {"price", "inventoryQuantity", "inventoryItem", "tracked", "currencyCode"}:
                                raise ShopifyError("Shopify GraphQL request failed")
                            try:
                                parent = partial
                                for segment in path[:-1]:
                                    parent = parent[segment]
                                if not isinstance(parent, dict) or path[-1] not in parent:
                                    raise ShopifyError("Invalid Shopify partial response")
                                parent[path[-1]] = None
                            except (KeyError, IndexError, TypeError):
                                raise ShopifyError("Invalid Shopify partial response") from None
                    if not retry:
                        data = payload.get("data")
                        if not isinstance(data, dict):
                            raise ShopifyError("Invalid Shopify response")
                        return data
                    throttle = payload.get("extensions", {}).get("cost", {})
                    state = throttle.get("throttleStatus", {})
                    rate = state.get("restoreRate", 0)
                    if rate > 0:
                        delay = min(max(delay, (throttle.get("requestedQueryCost", 0) - state.get("currentlyAvailable", 0)) / rate), 60)
            except (httpx.TimeoutException, httpx.NetworkError):
                retry = True
            except (httpx.HTTPError, ValueError, TypeError, AttributeError):
                raise ShopifyError("Shopify upstream request failed") from None
            if not retry or attempt == self.max_retries:
                raise ShopifyError("Shopify upstream unavailable") from None
            self.sleep(delay)
        raise ShopifyError("Shopify upstream unavailable")

    @staticmethod
    def _next(connection, seen):
        page = connection["pageInfo"]
        if not isinstance(page.get("hasNextPage"), bool):
            raise ShopifyError("Invalid Shopify pagination")
        if not page["hasNextPage"]:
            return None
        cursor = page.get("endCursor")
        if not isinstance(cursor, str) or not cursor or cursor in seen:
            raise ShopifyError("Invalid Shopify pagination cursor")
        seen.add(cursor)
        return cursor

    def list_products(self):
        from .main import Product
        if not self.publication_id:
            raise ShopifyError("SHOPIFY_PUBLICATION_ID is required")
        fields = 'pageInfo { hasNextPage endCursor } nodes { id title price inventoryQuantity inventoryItem { tracked } }'
        query = '''query Catalog($cursor: String, $publication: ID!) {
          shop { currencyCode }
          products(first: 20, after: $cursor, query: "status:active") {
            pageInfo { hasNextPage endCursor }
            nodes { id title description status publishedOnPublication(publicationId: $publication)
              variants(first: 20) { ''' + fields + ''' } }
          }
        }'''
        variant_query = '''query Variants($id: ID!, $cursor: String) {
          product(id: $id) { variants(first: 100, after: $cursor) { ''' + fields + ''' } }
        }'''
        products, seen, cursor = [], set(), None
        try:
            with httpx.Client(timeout=self.timeout, transport=self.transport, follow_redirects=False) as client:
                while True:
                    data = self._request(client, query, {"cursor": cursor, "publication": self.publication_id})
                    currency = data["shop"].get("currencyCode")
                    if currency is not None and not re.fullmatch(r"[A-Z]{3}", currency):
                        raise ShopifyError("Invalid Shopify currency")
                    connection = data["products"]
                    for item in connection["nodes"]:
                        if item.get("status") != "ACTIVE" or item.get("publishedOnPublication") is not True:
                            continue
                        variants, variant_seen = item["variants"], set()
                        while True:
                            for variant in variants["nodes"]:
                                try:
                                    price = Decimal(str(variant["price"])) if variant.get("price") is not None else None
                                    if price is not None and (not price.is_finite() or price < 0):
                                        price = None
                                except (InvalidOperation, ValueError):
                                    price = None
                                stock = variant.get("inventoryQuantity")
                                if (variant.get("inventoryItem") or {}).get("tracked") is not True or type(stock) is not int:
                                    stock = None
                                elif stock < 0:
                                    stock = 0
                                title = variant["title"]
                                products.append(Product(id=variant["id"], name=item["title"] + (" / " + title if title != "Default Title" else ""),
                                                        description=item.get("description") or "", price=price if currency else None,
                                                        currency=currency, stock=stock))
                            variant_cursor = self._next(variants, variant_seen)
                            if variant_cursor is None:
                                break
                            variants = self._request(client, variant_query, {"id": item["id"], "cursor": variant_cursor})["product"]["variants"]
                    cursor = self._next(connection, seen)
                    if cursor is None:
                        break
        except ShopifyError:
            raise
        except (KeyError, TypeError, ValueError, AttributeError):
            raise ShopifyError("Invalid Shopify catalog response") from None
        # Concurrent catalog edits can repeat nodes across pages; return unique variant IDs.
        return list({p.id: p for p in products}.values())
