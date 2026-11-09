"""Protocol E2E: official MCP client launches and communicates with a real stdio child."""
import json
import os
import sys
from pathlib import Path

import anyio
import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parents[1]


def run_session(check, *, code=None, env=None):
    async def run():
        child_env = dict(os.environ)
        for name in ('CATALOG_ADAPTER', 'SHOPIFY_SHOP', 'SHOPIFY_ADMIN_TOKEN', 'SHOPIFY_PUBLICATION_ID', 'AGENT_API_KEY'):
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
        assert data(await session.call_tool('search', {'q': 'usb', 'limit': 1}))['total'] == 2
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
