"""Protocol E2E: official MCP client launches and communicates with a real stdio child."""
import json
import os
import sys
from pathlib import Path

import anyio
import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from examples.procurement_agent import procure

ROOT = Path(__file__).resolve().parents[1]


def run_session(check, *, code=None, env=None):
    async def run():
        child_env = dict(os.environ)
        for name in ('CATALOG_ADAPTER', 'SHOPIFY_SHOP', 'SHOPIFY_ADMIN_TOKEN',
                     'SHOPIFY_CLIENT_ID', 'SHOPIFY_CLIENT_SECRET', 'SHOPIFY_API_VERSION',
                     'SHOPIFY_PUBLICATION_ID', 'AGENT_API_KEY', 'AGENT_RATE_LIMIT'):
            child_env.pop(name, None)
        child_env.update(env or {})
        parameters = StdioServerParameters(command=sys.executable,
            args=['-c', code] if code else ['-m', 'app.mcp_server'], cwd=str(ROOT), env=child_env)
        with anyio.fail_after(25):
            async with stdio_client(parameters) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    await check(session)
    anyio.run(run)


def data(result):
    assert not result.isError
    return result.structuredContent or json.loads(result.content[0].text)


def test_mcp_three_tools_e2e():
    async def check(session):
        tools = (await session.list_tools()).tools
        assert {t.name for t in tools} == {'discover', 'search', 'quote'}
        assert all(t.annotations.readOnlyHint for t in tools)
        assert data(await session.call_tool('discover'))['checkout_supported'] is False
        search = data(await session.call_tool('search', {'q': 'usb', 'limit': 1}))
        assert search['total'] == 2
        assert set(search['items'][0]) == {'id', 'name', 'description', 'price', 'currency', 'stock'}
        quote = data(await session.call_tool('quote', {'product_id': 'sku-001', 'quantity': 2}))
        assert quote['total_price'] == '7960' and quote['available'] is True
        assert not quote['inventory_reserved'] and not quote['binding']
        for name, arguments in [
            ('quote', {'product_id': 'missing', 'quantity': 1}),
            ('quote', {'product_id': 'x', 'quantity': 0}),
            ('quote', {'product_id': 'x', 'quantity': True}),
            ('search', {'q': 'private-secret' * 30}),
            ('search', {'limit': 101}), ('discover', {'token': 'private-secret'}),
            ('checkout', {}),
        ]:
            result = await session.call_tool(name, arguments)
            assert result.isError
            assert 'private-secret' not in str(result)
        assert data(await session.call_tool('search'))['total'] == 3
    run_session(check, env={'AGENT_API_KEY': 'a' * 32})


def test_mcp_unknown_price_and_inventory_e2e():
    code = '''
from app.main import Product, create_app
from app.mcp_server import create_mcp
class Unknown:
    def list_products(self):
        return [Product(id='unknown', name='Unknown', price=None, currency=None, stock=None)]
create_mcp(create_app(Unknown())).run(transport='stdio')
'''
    async def check(session):
        result = data(await session.call_tool('quote', {'product_id': 'unknown', 'quantity': 2}))
        assert result['unit_price'] is None and result['total_price'] is None
        assert result['available'] is None
    run_session(check, code=code)


def test_mcp_rate_limit_e2e():
    code = '''
from app.main import create_app
from app.mcp_server import create_mcp
create_mcp(create_app(requests_per_minute=1)).run(transport='stdio')
'''
    async def check(session):
        assert not (await session.call_tool('discover')).isError
        assert (await session.call_tool('search')).isError
    run_session(check, code=code)


def test_mcp_rejects_wrong_inherited_key():
    code = '''
from app.main import create_app
from app.mcp_server import create_mcp
create_mcp(create_app(api_key='b'*32)).run(transport='stdio')
'''
    async def check(session):
        result = await session.call_tool('discover')
        assert result.isError and 'bbbbbbbb' not in str(result)
    run_session(check, code=code, env={'AGENT_API_KEY': 'a' * 32})


def test_mcp_shopify_timeout_e2e():
    code = '''
import httpx
from app.main import create_app
from app.shopify import ShopifyAdapter
from app.mcp_server import create_mcp
def handler(request):
    raise httpx.ReadTimeout('private-shopify-token', request=request)
adapter = ShopifyAdapter('demo.myshopify.com', 'private-shopify-token',
    publication_id='gid://shopify/Publication/1', transport=httpx.MockTransport(handler),
    sleep=lambda _: None, max_retries=1)
create_mcp(create_app(adapter)).run(transport='stdio')
'''
    async def check(session):
        result = await session.call_tool('search')
        assert result.isError and 'private-shopify-token' not in str(result)
    run_session(check, code=code, env={'AGENT_API_KEY': 'a' * 32})


def test_mcp_shopify_client_credentials_e2e():
    code = '''
import httpx
import json
from app.main import create_app
from app.mcp_server import create_mcp

exchanges = 0

def handler(request):
    global exchanges
    if request.url.path == '/admin/oauth/access_token':
        exchanges += 1
        assert exchanges == 1
        return httpx.Response(200, json={'access_token': 'mock-token', 'expires_in': 3600})
    assert request.headers['X-Shopify-Access-Token'] == 'mock-token'
    query = json.loads(request.content)['query']
    if 'selectedOptions' in query:
        assert 'products(first: 5,' in query and 'variants(first: 10)' in query
        assert 'customProcurement: metafield(namespace: "custom", key: "procurement_specifications")' in query
    else:
        assert 'products(first: 20,' in query and 'variants(first: 20)' in query
    page = {'hasNextPage': False}
    variant = {'id': 'v1', 'title': '24 V', 'price': '4600', 'inventoryQuantity': 0,
               'inventoryItem': {'tracked': True}, 'sku': 'DC24',
               'selectedOptions': [{'name': 'Voltage', 'value': '24 V'}]}
    stocked = {**variant, 'id': 'v2', 'title': '12 V', 'price': '4200', 'inventoryQuantity': 18,
               'sku': 'DC12', 'selectedOptions': [{'name': 'Voltage', 'value': '12 V'}],
               'procurement': None, 'customProcurement': {'type': 'json', 'value': json.dumps({'mpn': 'PS-12', 'capacity': '60 W'})}}
    product = {'id': 'p1', 'title': 'DC supply', 'status': 'ACTIVE', 'publishedOnPublication': True,
               'variants': {'pageInfo': page, 'nodes': [variant, stocked]}}
    hidden = {**product, 'id': 'p2', 'title': 'Hidden supply', 'publishedOnPublication': False}
    return httpx.Response(200, json={'data': {'shop': {'currencyCode': 'JPY'},
        'products': {'pageInfo': page, 'nodes': [product, hidden]}}})

app = create_app()
app.state.catalog.transport = httpx.MockTransport(handler)
create_mcp(app).run(transport='stdio')
'''

    async def check(session):
        assert {t.name for t in (await session.list_tools()).tools} == {'discover', 'search', 'quote'}
        assert data(await session.call_tool('discover'))['data_source'] == 'ShopifyAdapter'
        items = data(await session.call_tool('search', {'q': 'supply'}))['items']
        assert len(items) == 2 and items[0]['name'] == 'DC supply / 24 V'
        assert items[0]['stock'] == 0 and items[0]['price'] == '4600'
        quote = data(await session.call_tool('quote', {'product_id': items[0]['id'], 'quantity': 2}))
        assert quote['total_price'] == '9200' and quote['currency'] == 'JPY'
        assert quote['available'] is False and not quote['binding'] and not quote['inventory_reserved']
        for request, expected in [
            ('DC supplyを、型番PS-12、電圧12000 mV、容量60 W、2個見積もって', 'quoted'),
            ('型番PS-12、2個見積もって', 'quoted'),
            ('DC supplyを、電圧24 V、2個見積もって', 'insufficient_stock'),
            ('DC supplyを、電圧24 V、容量60 W、2個見積もって', 'unknown'),
            ('DC supplyを、電圧48 V、2個見積もって', 'no_match'),
            ('Hidden supplyを、2個見積もって', 'no_match'),
        ]:
            result = await procure(session, request)
            assert result['status'] == expected
            assert result['metrics']['search']['calls'] == 1
            assert result['metrics']['search']['success_rate'] == 1
            assert result['metrics']['search']['elapsed_ms'] >= 0
            if expected == 'quoted':
                reference = result['reference_quotes'][0]
                assert (reference['product_id'], reference['variant_id'], reference['total_price']) == ('p1', 'v2', '8400')
                assert reference['retrieved_at'].endswith('+00:00')
                assert result['metrics']['quote']['calls'] == 1
            else:
                assert result['metrics']['quote']['calls'] == 0
                assert not result['reference_quotes']

    run_session(check, code=code, env={
        'CATALOG_ADAPTER': 'shopify', 'SHOPIFY_SHOP': 'demo.myshopify.com',
        'SHOPIFY_PUBLICATION_ID': 'gid://shopify/Publication/1', 'SHOPIFY_CLIENT_ID': 'mock-client',
        'SHOPIFY_CLIENT_SECRET': 'mock-secret', 'AGENT_API_KEY': 'a' * 32,
    })
