from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.main import create_app
from app.security import Guard
from app.shopify import ShopifyAdapter

KEY = 'a' * 32


@pytest.mark.parametrize('path', ['/discover', '/search', '/docs', '/openapi.json', '/redoc'])
def test_auth_required(path):
    c = TestClient(create_app(api_key=KEY))
    assert c.get(path).status_code == 401
    assert c.get(path, headers={'Authorization': 'Bearer wrong'}).status_code == 401
    assert c.get(path, headers={'Authorization': 'Bearer ' + KEY}).status_code == 200


def test_quote_auth_and_health():
    c = TestClient(create_app(api_key=KEY))
    assert c.post('/quote', json={}).status_code == 401
    assert c.get('/health').status_code == 200


def test_shopify_requires_auth_key():
    with pytest.raises(RuntimeError, match='AGENT_API_KEY'):
        create_app(ShopifyAdapter('demo.myshopify.com', 'secret'))


def test_rate_limit_and_audit(caplog):
    c = TestClient(create_app(api_key=KEY, requests_per_minute=2))
    c.headers['Authorization'] = 'Bearer ' + KEY
    with caplog.at_level('INFO'):
        assert c.get('/search?q=private-search').status_code == 200
        assert c.get('/discover').status_code == 200
        r = c.post('/quote', json={'product_id': 'sku-001', 'quantity': 1})
    assert r.status_code == 429 and 'Retry-After' in r.headers
    audit_text = '\n'.join(r.getMessage() for r in caplog.records if r.name == 'agent_ready.audit')
    assert KEY not in caplog.text and 'private-search' not in audit_text
    assert 'operation=quote outcome=429' in caplog.text


def test_rate_limit_reset_and_concurrency():
    now = [0]
    guard = Guard(KEY, requests_per_minute=5, clock=lambda: now[0])
    def call(_):
        try:
            guard.check(KEY, 'client')
            return True
        except HTTPException:
            return False
    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(call, range(20))) == 5
    now[0] = 60
    assert call(0)


@pytest.mark.parametrize('body', [
    {'product_id': '', 'quantity': 1}, {'product_id': 'x' * 257, 'quantity': 1},
    {'product_id': 'x', 'quantity': True}, {'product_id': 'x', 'quantity': '1'},
    {'product_id': 'x', 'quantity': 101}, {'product_id': 'x', 'quantity': 1, 'token': 'secret'},
])
def test_input_validation_redacted(body):
    r = TestClient(create_app()).post('/quote', json=body)
    assert r.status_code == 422 and r.json() == {'detail': 'Invalid request'}


def test_search_limits():
    c = TestClient(create_app())
    for params in [{'q': 'x' * 201}, {'limit': 0}, {'limit': 101}]:
        assert c.get('/search', params=params).status_code == 422
