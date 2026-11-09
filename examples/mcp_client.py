"""Run from the repository root: python -m examples.mcp_client."""
import asyncio
import os
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def main():
    params = StdioServerParameters(command=sys.executable, args=['-m', 'app.mcp_server'],
                                  cwd=str(Path(__file__).resolve().parents[1]), env=dict(os.environ))
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            for name, arguments in [
                ('discover', {}), ('search', {'q': 'usb', 'limit': 5}),
                ('quote', {'product_id': 'sku-001', 'quantity': 2}),
            ]:
                result = await session.call_tool(name, arguments)
                print(name, result.model_dump_json())
                if result.isError:
                    raise RuntimeError('MCP tool failed')


if __name__ == '__main__':
    asyncio.run(main())
