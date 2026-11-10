"""Run from the repository root: python -m examples.mcp_client."""
import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def main(query='usb', quantity=2):
    params = StdioServerParameters(command=sys.executable, args=['-m', 'app.mcp_server'],
                                  cwd=str(Path(__file__).resolve().parents[1]), env=dict(os.environ))
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            async def call(name, arguments):
                result = await session.call_tool(name, arguments)
                if result.isError:
                    raise RuntimeError(f'MCP {name} failed')
                print(name, result.model_dump_json())
                return result.structuredContent or json.loads(result.content[0].text)

            await call('discover', {})
            search = await call('search', {'q': query, 'limit': 5})
            if not search['items']:
                raise RuntimeError('No published variant matched the query')
            await call('quote', {'product_id': search['items'][0]['id'], 'quantity': quantity})


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--query', default='usb')
    parser.add_argument('--quantity', type=int, default=2)
    args = parser.parse_args()
    asyncio.run(main(args.query, args.quantity))
