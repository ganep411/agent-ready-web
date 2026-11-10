"""Merchant-authorized, read-only Admin GraphQL catalog integration."""
import re
import math
import time
from decimal import Decimal, InvalidOperation
from threading import Lock
from time import monotonic

import httpx

from .specifications import shopify_details

class ShopifyError(RuntimeError):
    pass


def _graphql_error(error):
    extensions = error.get("extensions") or {}
    code = extensions.get("code")
    details = []
    if code in ("ACCESS_DENIED", "MAX_COST_EXCEEDED", "SHOP_INACTIVE",
                "INTERNAL_SERVER_ERROR", "undefinedField", "argumentNotAccepted",
                "variableMismatch", "variableRequiresValidType", "invalidValue"):
        details.append(code)
    path = error.get("path")
    field = path[-1] if isinstance(path, list) and path else extensions.get("fieldName")
    if field in ("shop", "currencyCode", "products", "product", "status", "description",
                 "publishedOnPublication", "variants", "price", "inventoryQuantity", "inventoryItem", "tracked"):
        details.append(field)
    message = error.get("message")
    if code == "ACCESS_DENIED" and isinstance(message, str):
        details.extend(scope for scope in ("read_products", "read_inventory", "read_publications", "read_product_listings")
                       if scope in message)
    return "Shopify GraphQL request failed" + (f" ({', '.join(details)})" if details else "")


class _ClientCredentials:
    def __init__(self, url, client_id, client_secret):
        self._url = url
        self._form = {
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
        }
        self._lock = Lock()
        self._cached = None

    def get(self, client, rejected=None):
        with self._lock:
            if rejected is not None and self._cached is rejected:
                self._cached = None
            if self._cached and monotonic() < self._cached[1]:
                return self._cached

            started = monotonic()
            try:
                response = client.post(self._url, data=self._form)
                response.raise_for_status()
                payload = response.json()
                token, ttl = payload["access_token"], payload["expires_in"]
                if (not isinstance(token, str) or not token or not token.isascii()
                        or any(not 33 <= ord(c) <= 126 for c in token)
                        or type(ttl) is not int or ttl <= 0):
                    raise ValueError
                refresh_at = started + ttl - min(60, ttl / 10)
                if started + ttl <= monotonic():
                    raise ValueError
            except httpx.HTTPStatusError as exc:
                raise ShopifyError(f"Shopify token request failed (HTTP {exc.response.status_code})") from None
            except (httpx.HTTPError, ValueError, KeyError, TypeError, OverflowError):
                raise ShopifyError("Shopify token request failed") from None

            # The tuple identity distinguishes generations, even if Shopify returns the same token.
            self._cached = (token, refresh_at)
            return self._cached


class ShopifyAdapter:
    def __init__(self, shop, token=None, api_version="2026-10", transport=None,
                 publication_id=None, timeout=12, max_retries=3, sleep=time.sleep, *,
                 client_id=None, client_secret=None):
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]*\.myshopify\.com", shop):
            raise ValueError("SHOPIFY_SHOP must be a *.myshopify.com hostname")
        if not re.fullmatch(r"20\d{2}-(01|04|07|10)", api_version):
            raise ValueError("Invalid Shopify API version")
        if token and any(c in token for c in '\r\n'):
            raise ValueError("Invalid Shopify token")
        if not token and not (client_id and client_secret):
            raise ValueError("Set SHOPIFY_ADMIN_TOKEN or both SHOPIFY_CLIENT_ID and SHOPIFY_CLIENT_SECRET")
        if publication_id is not None and not re.fullmatch(r"gid://shopify/Publication/\d+", publication_id):
            raise ValueError("Invalid publication ID")
        if not 0 <= max_retries <= 5 or timeout <= 0:
            raise ValueError("Invalid request limits")
        self.url = f"https://{shop}/admin/api/{api_version}/graphql.json"
        self._token = token
        self._credentials = None if token else _ClientCredentials(
            f"https://{shop}/admin/oauth/access_token", client_id, client_secret)
        self.publication_id = publication_id
        self.transport, self.timeout = transport, timeout
        self.max_retries, self.sleep = max_retries, sleep

    def _request(self, client, query, variables):
        refreshed = False
        body = {"query": query, "variables": variables}
        for attempt in range(self.max_retries + 1):
            delay = min(2 ** attempt, 10)
            retry = False
            try:
                if self._credentials is None:
                    token = self._token
                else:
                    lease = self._credentials.get(client)
                    token = lease[0]
                response = client.post(self.url, headers={"X-Shopify-Access-Token": token}, json=body)
                if response.status_code == 401 and self._credentials is not None and not refreshed:
                    refreshed = True
                    token, _ = self._credentials.get(client, rejected=lease)
                    response = client.post(self.url, headers={"X-Shopify-Access-Token": token}, json=body)
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
                            if not isinstance(partial, dict) or not isinstance(path, list) or not path or path[-1] not in {"price", "inventoryQuantity", "inventoryItem", "tracked", "currencyCode", "procurement", "customProcurement"}:
                                raise ShopifyError(_graphql_error(error))
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
            except httpx.HTTPStatusError as exc:
                raise ShopifyError(f"Shopify GraphQL request failed (HTTP {exc.response.status_code})") from None
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

    def list_procurement_products(self):
        return self.list_products(include_details=True)

    def list_products(self, *, include_details=False):
        from .main import ProcurementProduct, Product
        if not self.publication_id:
            raise ShopifyError("SHOPIFY_PUBLICATION_ID is required")
        details_fields = ''' sku selectedOptions { name value }
            procurement: metafield(namespace: "procurement", key: "specifications") { type value }
            customProcurement: metafield(namespace: "custom", key: "procurement_specifications") { type value }''' if include_details else ""
        fields = 'pageInfo { hasNextPage endCursor } nodes { id title price inventoryQuantity inventoryItem { tracked }' + details_fields + ' }'
        page_size, first_variants, next_variants = (5, 10, 50) if include_details else (20, 20, 100)
        query = ('''query Catalog($cursor: String, $publication: ID!) {
          shop { currencyCode }
          products(first: PAGE_SIZE, after: $cursor, query: "status:active") {
            pageInfo { hasNextPage endCursor }
            nodes { id title description status publishedOnPublication(publicationId: $publication)
              variants(first: FIRST_VARIANTS) { ''' + fields + ''' } }
          }
        }''').replace("PAGE_SIZE", str(page_size)).replace("FIRST_VARIANTS", str(first_variants))
        variant_query = ('''query Variants($id: ID!, $cursor: String) {
          product(id: $id) { variants(first: NEXT_VARIANTS, after: $cursor) { ''' + fields + ''' } }
        }''').replace("NEXT_VARIANTS", str(next_variants))
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
                                values = dict(id=variant["id"], name=item["title"] + (" / " + title if title != "Default Title" else ""),
                                              description=item.get("description") or "", price=price if currency else None,
                                              currency=currency, stock=stock)
                                products.append(ProcurementProduct(**values, **shopify_details(item, variant))
                                                if include_details else Product(**values))
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
