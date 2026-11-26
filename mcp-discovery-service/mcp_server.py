"""
MCP Protocol Server Implementation
Exposes discovery tools via MCP protocol on port 8082
"""
import json
import logging

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse

logger = logging.getLogger(__name__)

# TODO: investigate intermittent 400 errors when lcore connects to mcp-discovery-server (seemingly during discovery sync)


class MCPDiscoveryServer:
    """MCP server exposing discovery tools"""

    def __init__(self, indexer):
        self.indexer = indexer

        # Disable DNS rebinding protection for cluster/container environments
        # In Kubernetes/OpenShift, security is handled by network policies and RBAC
        # This avoids issues with service hostnames, FQDN variations, and port mappings
        transport_security = TransportSecuritySettings(
            enable_dns_rebinding_protection=False
        )

        # FastMCP - handles both SSE and Streamable HTTP automatically
        self.mcp = FastMCP("mcp-discovery-server",
                           transport_security=transport_security)
        self._register_tools()
        self._register_custom_routes()

    def _register_tools(self):
        """Register MCP discovery tools using FastMCP"""

        @self.mcp.tool()
        async def search_mcp_tools(query: str, limit: int = 10) -> str:
            """Search for MCP tools by keyword in name or description.

            Args:
                query: Search query to find MCP tools
                limit: Maximum number of results (default: 10)
            """
            results, method = await self.indexer.search(query, limit)
            results_json = json.dumps([cap.model_dump()
                                      for cap in results], indent=2)
            return f"Found {len(results)} MCP tools matching '{query}' (via {method}):\n\n{results_json}"

        @self.mcp.tool()
        async def list_all_capabilities(server_name: str | None = None) -> str:
            """List all discovered MCP capabilities, optionally filtered by server name.

            Args:
                server_name: Optional filter by MCP server name
            """
            capabilities = self.indexer.capabilities

            if server_name:
                capabilities = [
                    cap for cap in capabilities
                    if cap.server_name == server_name
                ]

            capabilities_json = json.dumps(
                [cap.model_dump() for cap in capabilities], indent=2)
            return f"Total MCP capabilities: {len(capabilities)}\nLast refresh: {self.indexer.last_refresh_time.isoformat()}\n\n{capabilities_json}"

        @self.mcp.tool()
        async def get_tool_schema(tool_name: str, server_name: str | None = None) -> str:
            """Get detailed schema for a specific MCP tool.

            Args:
                tool_name: Name of the tool
                server_name: Optional MCP server name
            """
            found_cap = None
            for cap in self.indexer.capabilities:
                if cap.tool_name == tool_name:
                    if server_name and cap.server_name != server_name:
                        continue
                    found_cap = cap
                    break

            if not found_cap:
                return f"Tool '{tool_name}' not found"

            schema_json = json.dumps(found_cap.model_dump(), indent=2)
            return f"Tool Schema for '{tool_name}':\n\n{schema_json}"

        # TODO build out annoations for all MCP endpoints. At minimum name and description to make this explicit lookup. We should follow all the specs/best practices for this.
        # TODO return score from vector DB (ie 1 to -1) for clarity how the vector db is working when tracing tool calls
        @self.mcp.tool(
            name="recommend_tools",
            description="Get tool recommendations based on a task description.",
        )
        async def recommend_tools(task: str, limit: int = 5) -> str:
            """Get tool recommendations based on a task description.

            Args:
                task: Description of the task to find tools for
                limit: Maximum number of recommendations (default: 5)
            """
            results, method = await self.indexer.search(task, limit)

            if not results:
                return f"No tool recommendations found for task: '{task}'"

            results_json = json.dumps([cap.model_dump()
                                      for cap in results], indent=2)
            return f"Recommended {len(results)} tools for task '{task}' (via {method}):\n\n{results_json}"

    def _register_custom_routes(self):
        """Register custom HTTP routes like health checks"""

        @self.mcp.custom_route("/health", methods=["GET"])
        async def health_check(request: Request) -> JSONResponse:
            """Health check endpoint for Kubernetes probes"""
            return JSONResponse({
                "status": "healthy",
                "total_capabilities": len(self.indexer.capabilities),
                "last_refresh": self.indexer.last_refresh_time.isoformat(),
                "server": "mcp-discovery-service",
                "protocol": "mcp"
            })

    def create_app(self):
        """Create the ASGI app with session manager lifecycle"""
        import contextlib

        # Get the streamable HTTP app
        app = self.mcp.streamable_http_app()

        # Add lifespan manager for session manager
        @contextlib.asynccontextmanager
        async def lifespan(app):
            async with self.mcp.session_manager.run():
                logger.info("FastMCP session manager started")
                yield
                logger.info("FastMCP session manager stopped")

        # Attach lifespan to app
        app.router.lifespan_context = lifespan

        return app
