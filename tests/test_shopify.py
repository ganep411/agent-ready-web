import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.shopify import ShopifyAdapter, ShopifyError

KEY = 'a' * 32
PUB = 'gid://shopify/Publication/1'


def variant(id='v1', **changes):
    return dict(id=id, title='Default Title', price='3.50', inventoryQuantity=4,
                inventoryItem={'tracked': True}, **changes)


def connection(nodes, cursor=None):
    return {'nodes': nodes, 'pageInfo': {'hasNextPage': cursor is not None, 'endCursor': cursor}}


def product(id='p1', variants=None, **changes):
    item = dict(id=id, title='Notebook', description='Paper', status='ACTIVE',
                publishedOnPublication=True, variants=variants or connection([variant()]))
    item.update(changes)
    return item


def payload(items=None, cursor=None, currency='JPY'):
    return {'data': {'shop': {'currencyCode': currency}, 'products': connection(items if items is not None else [product()], cursor)}}


def adapter(handler, **kwargs):
    return ShopifyAdapter('demo.myshopify.com', 'shopify-secret', publication_id=PUB,
                          transport=httpx.MockTransport(handler), sleep=lambda _: None, **kwargs)


def client(a):
    c = TestClient(create_app(a, api_key=KEY))
    c.headers['Authorization'] = 'Bearer ' + KEY
    return c


def test_product_and_variant_pagination():
    requests = []
    def handler(req):
        body = json.loads(req.content)
        requests.append(body)
        assert req.headers['X-Shopify-Access-Token'] == 'shopify-secret'
        if 'query Variants' in body['query']:
            assert body['variables'] == {'id': 'p1', 'cursor': 'v-next'}
            return httpx.Response(200, json={'data': {'product': {'variants': connection([dict(variant('v2'), title='Blue')])}}})
        assert body['variables']['publication'] == PUB
        if body['variables']['cursor'] is None:
            return httpx.Response(200, json=payload([product(variants=connection([variant()], 'v-next'))], 'p-next'))
        assert body['variables']['cursor'] == 'p-next'
        return httpx.Response(200, json=payload([product('p2', connection([variant('v3')]))]))
    result = adapter(handler).list_products()
    assert [p.id for p in result] == ['v1', 'v2', 'v3']
    assert result[1].name == 'Notebook / Blue'
    assert len(requests) == 3


def test_no_thousand_product_cap():
    count = 0
    def handler(req):
        nonlocal count
        count += 1
        # 51 pages of 20 products exercise the old 1000-product boundary.
        items = [product(f'p{count}-{i}', connection([variant(f'v{count}-{i}')])) for i in range(20)]
        return httpx.Response(200, json=payload(items, str(count) if count < 51 else None))
    assert len(adapter(handler).list_products()) == 1020


def test_more_than_one_hundred_variants():
    count = 0
    def handler(req):
        nonlocal count
        count += 1
        nodes = [variant(str(i)) for i in range((count - 1) * 20, min(count * 20, 105))]
        variants = connection(nodes, str(count) if count < 6 else None)
        if count == 1:
            return httpx.Response(200, json=payload([product(variants=variants)]))
        return httpx.Response(200, json={'data': {'product': {'variants': variants}}})
    assert len(adapter(handler).list_products()) == 105


@pytest.mark.parametrize('changes', [{'status': 'DRAFT'}, {'status': 'ARCHIVED'}, {'publishedOnPublication': False}, {'publishedOnPublication': None}])
def test_publication_filter(changes):
    assert adapter(lambda _: httpx.Response(200, json=payload([product(**changes)]))).list_products() == []


def test_missing_publication_fails_closed():
    a = ShopifyAdapter('demo.myshopify.com', 'secret')
    with pytest.raises(ShopifyError, match='PUBLICATION'):
        a.list_products()


@pytest.mark.parametrize('price', [None, 'not-money', '-1', 'NaN', 'Infinity'])
def test_unknown_price(price):
    item = product(variants=connection([dict(variant(), price=price)]))
    c = client(adapter(lambda _: httpx.Response(200, json=payload([item]))))
    response = c.post('/quote', json={'product_id': 'v1', 'quantity': 2})
    assert response.status_code == 200
    assert response.json()['unit_price'] is None
    assert response.json()['total_price'] is None


@pytest.mark.parametrize('stock,inventory,expected', [(None, {'tracked': True}, None), (4, {'tracked': False}, None), (4, None, None), (0, {'tracked': True}, False), (-2, {'tracked': True}, False)])
def test_inventory_semantics(stock, inventory, expected):
    item = product(variants=connection([dict(variant(), inventoryQuantity=stock, inventoryItem=inventory)]))
    response = client(adapter(lambda _: httpx.Response(200, json=payload([item])))).post('/quote', json={'product_id': 'v1', 'quantity': 1})
    assert response.json()['available'] is expected


def test_unknown_currency():
    p = adapter(lambda _: httpx.Response(200, json=payload(currency=None))).list_products()[0]
    assert p.currency is None and p.price is None


@pytest.mark.parametrize('failure', ['timeout', 'network', '429', '503', 'throttled'])
def test_retry_then_success(failure):
    calls, waits = [], []
    def handler(req):
        calls.append(req)
        if len(calls) == 1:
            if failure == 'timeout':
                raise httpx.ReadTimeout('shopify-secret', request=req)
            if failure == 'network':
                raise httpx.ConnectError('shopify-secret', request=req)
            if failure == 'throttled':
                return httpx.Response(200, json={'errors': [{'extensions': {'code': 'THROTTLED'}}]})
            return httpx.Response(int(failure), headers={'Retry-After': '2'})
        return httpx.Response(200, json=payload())
    a = adapter(handler)
    a.sleep = waits.append
    assert len(a.list_products()) == 1
    assert len(calls) == 2 and len(waits) == 1


@pytest.mark.parametrize('failure', ['timeout', '429', '401', 'graphql', 'json', 'partial'])
def test_failures_are_bounded_and_redacted(failure, caplog):
    calls = []
    def handler(req):
        calls.append(req)
        if failure == 'timeout':
            raise httpx.ReadTimeout('shopify-secret', request=req)
        if failure in ('429', '401'):
            return httpx.Response(int(failure), text='shopify-secret')
        if failure == 'graphql':
            return httpx.Response(200, json={'errors': [{'message': 'shopify-secret'}]})
        if failure == 'partial':
            return httpx.Response(200, json=dict(payload(), errors=[{'message': 'denied'}]))
        return httpx.Response(200, text='shopify-secret')
    with caplog.at_level('INFO'):
        r = client(adapter(handler, max_retries=2)).get('/search')
    assert r.status_code == 502
    assert 'shopify-secret' not in r.text + caplog.text
    assert len(calls) == (3 if failure in ('timeout', '429') else 1)


@pytest.mark.parametrize('variant_pages', [True, False])
def test_repeated_cursor_rejected(variant_pages):
    def handler(req):
        if variant_pages:
            if 'query Variants' in json.loads(req.content)['query']:
                return httpx.Response(200, json={'data': {'product': {'variants': connection([variant()], 'same')}}})
            return httpx.Response(200, json=payload([product(variants=connection([variant()], 'same'))]))
        return httpx.Response(200, json=payload(cursor='same'))
    with pytest.raises(ShopifyError, match='cursor'):
        adapter(handler).list_products()


def test_invalid_and_missing_cursor():
    data = payload(cursor='next')
    data['data']['products']['pageInfo']['endCursor'] = None
    with pytest.raises(ShopifyError):
        adapter(lambda _: httpx.Response(200, json=data)).list_products()


@pytest.mark.parametrize('shop', ['evil.myshopify.com.evil.org', 'x.myshopify.com:443', 'x@a.myshopify.com', 'x/myshopify.com', 'x.myshopify.com\n'])
def test_shop_hostname(shop):
    with pytest.raises(ValueError):
        ShopifyAdapter(shop, 'secret')


def test_timeouts_and_redirects():
    def handler(req):
        assert req.extensions['timeout'] == dict(connect=7, read=7, write=7, pool=7)
        return httpx.Response(302, headers={'Location': 'https://evil.example/'})
    with pytest.raises(ShopifyError):
        adapter(handler, timeout=7).list_products()


@pytest.mark.parametrize('field', ['price', 'inventoryQuantity', 'inventoryItem'])
def test_partial_field_error_becomes_unknown(field):
    response = payload()
    response['errors'] = [{'message': 'private-shopify-token', 'path': ['products', 'nodes', 0, 'variants', 'nodes', 0, field]}]
    c = client(adapter(lambda _: httpx.Response(200, json=response)))
    result = c.post('/quote', json={'product_id': 'v1', 'quantity': 1})
    assert result.status_code == 200
    assert 'private-shopify-token' not in result.text
    if field == 'price':
        assert result.json()['unit_price'] is None and result.json()['total_price'] is None
    else:
        assert result.json()['available'] is None


def test_graphql_cost_backoff():
    count, waits = 0, []
    def handler(req):
        nonlocal count
        count += 1
        if count == 1:
            return httpx.Response(200, json={
                'errors': [{'extensions': {'code': 'THROTTLED'}}],
                'extensions': {'cost': {'requestedQueryCost': 100, 'throttleStatus': {'restoreRate': 10, 'currentlyAvailable': 0}}}})
        return httpx.Response(200, json=payload())
    a = adapter(handler)
    a.sleep = waits.append
    assert len(a.list_products()) == 1 and waits == [10]
