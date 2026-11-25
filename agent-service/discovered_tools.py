import json
from contextlib import asynccontextmanager
from typing import Any

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.types import TextContent
from pydantic_ai import FunctionToolset, ModelRetry


DISCOVERY = "mcp-discovery-server"


@asynccontextmanager
async def connect(server, caller_headers):
    headers = {name: caller_headers[name] for name in server.get(
        "headers", []) if name in caller_headers}
    async with httpx.AsyncClient(headers=headers, follow_redirects=False, trust_env=False) as client:
        async with streamable_http_client(server["url"], http_client=client) as (reader, writer, _):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                yield session


def turn_tools(servers, caller_headers):
    recommended = set()

    async def recommend_tools(task: str, limit: int = 5) -> list[dict]:
        """Find tools for this task before trying to execute one."""
        recommended.clear()
        if DISCOVERY not in servers:
            return []
        async with connect(servers[DISCOVERY], caller_headers) as session:
            result = await session.call_tool("recommend_tools", {"task": task, "limit": limit})
        if result.isError or len(result.content) != 1 or not isinstance(result.content[0], TextContent):
            return []
        text = result.content[0].text
        if not text.startswith("Recommended "):
            return []
        try:
            capabilities = json.loads(text.rpartition("\n\n")[2])
        except ValueError:
            return []
        choices = []
        for item in capabilities if isinstance(capabilities, list) else []:
            if not isinstance(item, dict):
                continue
            server, tool = item.get("server_name"), item.get("tool_name")
            if isinstance(server, str) and server in servers and server != DISCOVERY and isinstance(tool, str) and tool:
                recommended.add((server, tool))
                choices.append({key: item[key] for key in ("server_name", "tool_name", "description", "input_schema")
                                if key in item})
        return choices

    async def execute_tool(server_name: str, tool_name: str, arguments: dict[str, Any]) -> str:
        """Call an exact recommended server and tool with its input arguments."""
        if (server_name, tool_name) not in recommended:
            raise ModelRetry("Select a tool recommended for this turn.")
        result = None
        async with connect(servers[server_name], caller_headers) as session:
            cursor = None
            while True:
                page = await session.list_tools(cursor=cursor)
                if any(tool.name == tool_name for tool in page.tools):
                    result = await session.call_tool(tool_name, arguments)
                    break
                cursor = page.nextCursor
                if not cursor:
                    break
        if result is None:
            raise ModelRetry("The recommended tool is no longer available.")
        if result.isError:
            raise ModelRetry("The downstream tool returned an error.")
        if result.structuredContent is not None:
            return json.dumps(result.structuredContent)
        return "\n".join(block.text for block in result.content if isinstance(block, TextContent))

    return FunctionToolset(tools=[recommend_tools, execute_tool])
