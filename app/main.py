"""Agent-ready storefront API. Read-only: no purchases or inventory holds."""
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Protocol, Annotated, Literal
from uuid import uuid4
from types import SimpleNamespace

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, Field, ConfigDict

from .shopify import ShopifyAdapter
from .security import Guard, audit

VERSION = "0.5.0"

class Product(BaseModel):
    id: str
    name: str
    description: str = ""
    price: Decimal | None = Field(default=None, ge=0, allow_inf_nan=False)
    currency: str | None = None
    stock: int | None = Field(default=None, ge=0)  # None = unknown, not zero

class Measurement(BaseModel):
    value: str
    unit: str
    source: Literal["variant_metafield", "variant_option", "demo"]

class ProcurementAttributes(BaseModel):
    voltage: Measurement | None = None
    capacity: Measurement | None = None

class ProcurementProduct(Product):
    product_id: str | None = None
    variant_id: str
    sku: str | None = None
    mpn: str | None = None
    attributes: ProcurementAttributes = Field(default_factory=ProcurementAttributes)

class SearchResponse(BaseModel):
    items: list[Product]
    total: int

class QuoteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    product_id: str = Field(min_length=1, max_length=256)
    quantity: int = Field(strict=True, ge=1, le=100)

class QuoteResponse(BaseModel):
    quote_id: str
    product_id: str
    quantity: int
    available: bool | None
    unit_price: Decimal | None
    total_price: Decimal | None
    currency: str | None = None
    expires_at: datetime
    terms: dict[str, str]
    binding: bool = False
    inventory_reserved: bool = False

class CatalogAdapter(Protocol):
    def list_products(self) -> list[Product]: ...

class DemoAdapter:
    def list_products(self):
        return [
            Product(id="sku-001", name="USB-C Charger 65W", description="Compact USB-C charger", price="3980", currency="JPY", stock=12),
            Product(id="sku-002", name="USB-C Cable 2m", description="Durable charging cable", price="1280", currency="JPY", stock=0),
            Product(id="sku-003", name="Wireless Mouse", description="Bluetooth wireless mouse", price="2480", currency="JPY", stock=8),
        ]

    def list_procurement_products(self):
        products = self.list_products()
        return [
            ProcurementProduct(**product.model_dump(), product_id=f"demo-product-{index}",
                               variant_id=product.id, sku=product.id)
            if index != 1 else ProcurementProduct(
                **product.model_dump(), product_id="demo-product-1", variant_id=product.id,
                sku=product.id, mpn="DEMO-65",
                attributes={"voltage": {"value": "20", "unit": "V", "source": "demo"},
                            "capacity": {"value": "65", "unit": "W", "source": "demo"}})
            for index, product in enumerate(products, start=1)
        ]

def create_app(adapter: CatalogAdapter | None = None, *, api_key: str | None = None, requests_per_minute: int | None = None):
    app = FastAPI(title="Agent-ready Web SDK MVP", version=VERSION)
    if adapter is None:
        mode = os.getenv("CATALOG_ADAPTER", "demo")
        if mode == "demo":
            adapter = DemoAdapter()
        elif mode == "shopify":
            shop, token = os.getenv("SHOPIFY_SHOP"), os.getenv("SHOPIFY_ADMIN_TOKEN")
            if not shop:
                raise RuntimeError("SHOPIFY_SHOP is required")
            if not os.getenv("SHOPIFY_PUBLICATION_ID"):
                raise RuntimeError("SHOPIFY_PUBLICATION_ID is required")
            adapter = ShopifyAdapter(shop, token, api_version=os.getenv("SHOPIFY_API_VERSION", "2026-10"),
                                     publication_id=os.getenv("SHOPIFY_PUBLICATION_ID"),
                                     client_id=os.getenv("SHOPIFY_CLIENT_ID"),
                                     client_secret=os.getenv("SHOPIFY_CLIENT_SECRET"))
        else:
            raise RuntimeError("Unknown CATALOG_ADAPTER")
    api_key = api_key if api_key is not None else os.getenv("AGENT_API_KEY")
    if isinstance(adapter, ShopifyAdapter) and not api_key:
        raise RuntimeError("AGENT_API_KEY is required for Shopify mode")
    if requests_per_minute is None:
        requests_per_minute = int(os.getenv("AGENT_RATE_LIMIT", "60"))
    app.state.guard = Guard(api_key, requests_per_minute)
    app.state.catalog = adapter

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request, exc):
        # FastAPI's default errors echo submitted values, which may contain secrets.
        return JSONResponse(status_code=422, content={"detail": "Invalid request"})

    @app.middleware("http")
    async def secure_api(request: Request, call_next):
        operation = {"/discover": "discover", "/search": "search", "/quote": "quote", "/docs": "docs", "/openapi.json": "openapi", "/redoc": "docs"}.get(request.url.path, "other")
        if request.url.path == "/health":
            return await call_next(request)
        try:
            header = request.headers.get("authorization", "")
            credential = header[7:] if header.startswith("Bearer ") else None
            app.state.guard.check(credential, "http:" + (request.client.host if request.client else "unknown"))
        except HTTPException as exc:
            audit(operation, str(exc.status_code))
            return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail}, headers=exc.headers)
        response = await call_next(request)
        audit(operation, str(response.status_code))
        return response

    def catalog(include_details=False):
        try:
            source = app.state.catalog
            if include_details and hasattr(source, "list_procurement_products"):
                return source.list_procurement_products()
            products = source.list_products()
            if include_details:
                return [ProcurementProduct(**product.model_dump(), variant_id=product.id) for product in products]
            return products
        except Exception:
            raise HTTPException(status_code=502, detail="Catalog temporarily unavailable") from None

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.get("/discover")
    def discover():
        return {"service_id": "agent-ready-store", "version": VERSION, "capabilities": [
            {"name": "discover", "method": "GET", "path": "/discover"},
            {"name": "search", "method": "GET", "path": "/search"},
            {"name": "quote", "method": "POST", "path": "/quote"}],
            "openapi": "/openapi.json", "documentation": "/docs", "checkout_supported": False,
            "data_source": type(app.state.catalog).__name__}

    @app.get("/search", response_model=SearchResponse)
    def search(q: Annotated[str, Query(max_length=200)] = "", limit: Annotated[int, Query(ge=1, le=100)] = 20):
        needle = q.casefold().strip()
        items = [p for p in catalog() if needle in (p.name + " " + p.description).casefold()]
        return SearchResponse(items=items[:limit], total=len(items))

    def procurement_search(q, limit):
        needle = q.casefold().strip()
        items = [p for p in catalog(include_details=True)
                 if any(needle in value.casefold() for value in (p.name, p.description, p.sku or "", p.mpn or ""))]
        return {"items": [p.model_dump(mode="json") for p in items[:limit]], "total": len(items)}

    @app.post("/quote", response_model=QuoteResponse)
    def quote(request: QuoteRequest):
        product = next((p for p in catalog() if p.id == request.product_id), None)
        if product is None:
            raise HTTPException(status_code=404, detail="Product not found")
        return QuoteResponse(quote_id=str(uuid4()), product_id=product.id, quantity=request.quantity,
            available=(product.stock >= request.quantity) if product.stock is not None else None,
            unit_price=product.price, total_price=product.price * request.quantity if product.price is not None else None, currency=product.currency,
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
            terms={"pricing": "Indicative only; taxes, shipping and discounts may differ at checkout", "inventory": "Not reserved", "availability": "null means inventory unknown", "unknown_values": "null price or currency means unknown"})
    app.state.operations = SimpleNamespace(discover=discover, search=search, quote=quote,
                                           procurement_search=procurement_search)
    return app

app = create_app()
