"""MCP stdio transport; inherits server-side configuration, never exposes tokens as tool arguments."""
import os
from typing import Annotated

from fastapi import HTTPException
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from pydantic import Field
from mcp.types import ToolAnnotations

from .main import QuoteRequest, create_app
from .security import audit


def create_mcp(app=None):
    app = app if app is not None else create_app()
    class SafeMCP(FastMCP):
        async def call_tool(self, name, arguments):
            operation = name if name in {"discover", "search", "quote"} else "other"
            try:
                app.state.guard.check(os.getenv("AGENT_API_KEY"), "mcp:stdio")
                schemas = {tool.name: tool.inputSchema for tool in await self.list_tools()}
                schema = schemas.get(name)
                if schema is None or set(arguments) - set(schema.get("properties", {})):
                    raise ToolError("Invalid arguments")
                result = await super().call_tool(name, arguments)
                audit(operation, "ok")
                return result
            except Exception:
                audit(operation, "error")
                # SDK validation errors include raw arguments unless intercepted here.
                raise ToolError("Request failed; check authentication, limits and arguments") from None

    server = SafeMCP("Agent-ready Web SDK", instructions="Read-only catalog. Quotes are indicative; no purchases, payments or inventory reservations.")

    def invoke(operation, **arguments):
        try:
            # stdio is a local, inherited trust boundary. It is not a network endpoint.
            result = getattr(app.state.operations, operation)(**arguments)
            return result.model_dump(mode="json") if hasattr(result, "model_dump") else result
        except HTTPException as exc:
            audit(operation, str(exc.status_code))
            raise ToolError(f"Request failed ({exc.status_code})") from None
        except Exception:
            audit(operation, "error")
            raise ToolError("Request failed") from None

    @server.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False))
    def discover() -> dict:
        """Discover read-only storefront capabilities."""
        return invoke("discover")

    @server.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False))
    def search(q: Annotated[str, Field(max_length=200)] = "",
               limit: Annotated[int, Field(strict=True, ge=1, le=100)] = 20) -> dict:
        """Search published product variants; null price/stock means unknown."""
        return invoke("search", q=q, limit=limit)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False))
    def quote(product_id: Annotated[str, Field(min_length=1, max_length=256)],
              quantity: Annotated[int, Field(strict=True, ge=1, le=100)]) -> dict:
        """Get an indicative quote without reserving inventory or executing payment."""
        return invoke("quote", request=QuoteRequest(product_id=product_id, quantity=quantity))

    return server


if __name__ == "__main__":
    create_mcp().run(transport="stdio")
