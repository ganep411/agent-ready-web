from decimal import Decimal
import httpx
import pytest
from fastapi.testclient import TestClient
from app.main import create_app, ShopifyAdapter

client = TestClient(create_app())

def test_discover():
    r = client.get('/discover')
    assert r.status_code == 200
    assert [x['name'] for x in r.json()['capabilities']] == ['discover', 'search', 'quote']

def test_search():
    r = client.get('/search', params={'q': 'usb'})
    assert r.status_code == 200 and r.json()['total'] == 2

def test_quote():
    r = client.post('/quote', json={'product_id': 'sku-001', 'quantity': 2})
    assert r.status_code == 200
    assert r.json()['available'] is True
    assert Decimal(r.json()['total_price']) == Decimal('7960')
    assert r.json()['inventory_reserved'] is False

def test_out_of_stock():
    assert client.post('/quote', json={'product_id': 'sku-002', 'quantity': 1}).json()['available'] is False

def test_unknown_product():
    assert client.post('/quote', json={'product_id': 'missing', 'quantity': 1}).status_code == 404

def test_invalid_quantity():
    assert client.post('/quote', json={'product_id': 'sku-001', 'quantity': 0}).status_code == 422

def test_unknown_inventory():
    class Unknown:
        def list_products(self):
            from app.main import Product
            return [Product(id='x', name='X', price='1.50', currency='USD', stock=None)]
    r = TestClient(create_app(Unknown())).post('/quote', json={'product_id': 'x', 'quantity': 1})
    assert r.json()['available'] is None

def test_shopify_mock():
    def handler(req):
        assert req.headers['X-Shopify-Access-Token'] == 'secret'
        return httpx.Response(200, json={'data': {'shop': {'currencyCode': 'JPY'}, 'products': {'pageInfo': {'hasNextPage': False, 'endCursor': None}, 'nodes': [{'title': 'Notebook', 'description': 'Paper', 'status': 'ACTIVE', 'publishedOnPublication': True, 'variants': {'pageInfo': {'hasNextPage': False}, 'nodes': [{'id': 'gid://shopify/ProductVariant/1', 'title': 'Default Title', 'price': '500', 'inventoryQuantity': 4, 'inventoryItem': {'tracked': True}}]}}]}}})
    adapter = ShopifyAdapter('demo.myshopify.com', 'secret', transport=httpx.MockTransport(handler), publication_id='gid://shopify/Publication/1')
    c = TestClient(create_app(adapter, api_key='a' * 32))
    c.headers['Authorization'] = 'Bearer ' + 'a' * 32
    assert c.get('/search', params={'q': 'notebook'}).json()['total'] == 1
    assert c.post('/quote', json={'product_id': 'gid://shopify/ProductVariant/1', 'quantity': 5}).json()['available'] is False

def test_shopify_failure():
    adapter = ShopifyAdapter('demo.myshopify.com', 'secret', transport=httpx.MockTransport(lambda req: httpx.Response(401)), publication_id='gid://shopify/Publication/1')
    assert TestClient(create_app(adapter, api_key='a' * 32)).get('/search', headers={'Authorization': 'Bearer ' + 'a' * 32}).status_code == 502

def test_invalid_shop():
    with pytest.raises(ValueError):
        ShopifyAdapter('evil.com/abc', 'secret')
