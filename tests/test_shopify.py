import json
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import parse_qs

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


def credentials_adapter(handler, **kwargs):
    return ShopifyAdapter('demo.myshopify.com', publication_id=PUB,
                          client_id='test-client', client_secret='test-client-secret',
                          transport=httpx.MockTransport(handler), **kwargs)


def test_client_credentials_http_catalog(monkeypatch):
    for name, value in {
        'CATALOG_ADAPTER': 'shopify', 'SHOPIFY_SHOP': 'demo.myshopify.com',
        'SHOPIFY_PUBLICATION_ID': PUB, 'SHOPIFY_CLIENT_ID': 'test-client',
        'SHOPIFY_CLIENT_SECRET': 'test-client-secret', 'AGENT_API_KEY': KEY,
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv('SHOPIFY_ADMIN_TOKEN', raising=False)
    calls = []
    variants = connection([
        {**variant('v1'), 'title': 'A'},
        {**variant('v2'), 'title': 'B', 'inventoryQuantity': 0, 'price': '4.00'},
    ])

    def handler(request):
        calls.append(request.url.path)
        assert request.url.host == 'demo.myshopify.com'
        if request.url.path == '/admin/oauth/access_token':
            assert request.headers['Content-Type'] == 'application/x-www-form-urlencoded'
            assert 'X-Shopify-Access-Token' not in request.headers
            assert parse_qs(request.content.decode()) == {
                'grant_type': ['client_credentials'], 'client_id': ['test-client'],
                'client_secret': ['test-client-secret'],
            }
            return httpx.Response(200, json={'access_token': 'test-token', 'expires_in': 86400})
        assert request.headers['X-Shopify-Access-Token'] == 'test-token'
        assert json.loads(request.content)['variables']['publication'] == PUB
        return httpx.Response(200, json=payload([
            product(variants=variants),
            product('p2', publishedOnPublication=False),
            product('p3', status='DRAFT'),
            product('p4', status='ARCHIVED'),
        ]))

    app = create_app()
    app.state.catalog.transport = httpx.MockTransport(handler)
    c = TestClient(app, headers={'Authorization': 'Bearer ' + KEY})
    assert c.get('/discover').json()['data_source'] == 'ShopifyAdapter'
    assert calls == []
    items = c.get('/search', params={'q': 'notebook'}).json()['items']
    assert [(p['id'], p['name'], p['price'], p['currency'], p['stock']) for p in items] == [
        ('v1', 'Notebook / A', '3.50', 'JPY', 4), ('v2', 'Notebook / B', '4.00', 'JPY', 0),
    ]
    for id, available, total in [('v1', True, '7.00'), ('v2', False, '8.00')]:
        quote = c.post('/quote', json={'product_id': id, 'quantity': 2}).json()
        assert (quote['available'], quote['total_price'], quote['currency']) == (available, total, 'JPY')
        assert not quote['binding'] and not quote['inventory_reserved']
    assert calls.count('/admin/oauth/access_token') == 1


def test_client_credentials_early_refresh_is_shared(monkeypatch):
    now = 1000
    monkeypatch.setattr('app.shopify.monotonic', lambda: now)
    exchanges, tokens = [], []

    def handler(request):
        if request.url.path.endswith('/access_token'):
            exchanges.append(request)
            return httpx.Response(200, json={'access_token': f'token-{len(exchanges)}', 'expires_in': 600})
        tokens.append(request.headers['X-Shopify-Access-Token'])
        return httpx.Response(200, json=payload())

    a = credentials_adapter(handler)
    a.list_products()
    now += 539
    a.list_products()
    now += 1
    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(lambda _: a.list_products(), range(12)))
    assert tokens == ['token-1'] * 2 + ['token-2'] * 12
    assert len(exchanges) == 2


@pytest.mark.parametrize('second_status', [200, 401, 403])
def test_client_credentials_401_replays_once(second_status):
    tokens = []
    exchanges = 0

    def handler(request):
        nonlocal exchanges
        if request.url.path.endswith('/access_token'):
            exchanges += 1
            return httpx.Response(200, json={'access_token': f'token-{exchanges}', 'expires_in': 600})
        tokens.append(request.headers['X-Shopify-Access-Token'])
        return httpx.Response(401 if len(tokens) == 1 else second_status, json=payload())

    a = credentials_adapter(handler, max_retries=0)
    if second_status == 200:
        assert a.list_products()
    else:
        with pytest.raises(ShopifyError):
            a.list_products()
    assert tokens == ['token-1', 'token-2'] and exchanges == 2


@pytest.mark.parametrize('status,body', [
    (302, {}), (401, {'message': 'test-client-secret'}),
    (200, {'access_token': 'test-token', 'expires_in': 0}),
    (200, {'access_token': 'test-token\r\n', 'expires_in': 600}),
])
def test_failed_token_exchange_is_redacted_and_retryable(status, body, caplog):
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if len(calls) == 1:
            return httpx.Response(status, json=body, headers={'Location': 'https://other.example/'})
        if request.url.path.endswith('/access_token'):
            return httpx.Response(200, json={'access_token': 'test-token', 'expires_in': 600})
        return httpx.Response(200, json=payload())

    a = credentials_adapter(handler)
    message = 'Shopify token request failed'
    if status != 200:
        message += f' (HTTP {status})'
    with pytest.raises(ShopifyError) as error:
        a.list_products()
    assert str(error.value) == message
    assert calls == ['/admin/oauth/access_token']
    assert a.list_products()
    assert 'test-client-secret' not in caplog.text and 'test-token' not in caplog.text


def test_fixed_token_takes_precedence_and_does_not_retry_401():
    calls = []

    def handler(request):
        calls.append(request.url.path)
        assert request.headers['X-Shopify-Access-Token'] == 'shopify-secret'
        return httpx.Response(401)

    a = adapter(handler, client_id='test-client', client_secret='test-client-secret')
    with pytest.raises(ShopifyError):
        a.list_products()
    assert calls == ['/admin/api/2026-10/graphql.json']


def test_incomplete_client_credentials_fail_closed():
    with pytest.raises(ValueError, match='SHOPIFY_CLIENT_SECRET'):
        ShopifyAdapter('demo.myshopify.com', client_id='test-client')


@pytest.mark.parametrize('code,path,expected', [
    ('ACCESS_DENIED', ['products', 'nodes', 0, 'publishedOnPublication'],
     ' (ACCESS_DENIED, publishedOnPublication, read_publications)'),
    ('MAX_COST_EXCEEDED', None, ' (MAX_COST_EXCEEDED)'),
    ('secret-code', ['secret-field'], ''),
])
def test_graphql_diagnostic_only_includes_known_labels(code, path, expected):
    def handler(request):
        return httpx.Response(200, json={'errors': [{
            'message': 'secret response text, requires read_publications',
            'path': path, 'extensions': {'code': code},
        }]})

    with pytest.raises(ShopifyError) as error:
        adapter(handler).list_products()
    assert str(error.value) == 'Shopify GraphQL request failed' + expected


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
