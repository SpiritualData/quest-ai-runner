"""A real MCP server for the write_through_mcp component: a child process that speaks MCP over stdio.

``python real_server.py <spec.json>``: the spec names the tools to serve, the results each one returns and which ones fail, and
the file where every ``tools/call`` it receives is appended as one JSON line. The calls are therefore written by the server
process itself, so the component counts what really crossed the wire, not what a fake client was handed."""
import asyncio
import json
import sys

from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server


def main(spec_path):
    spec = json.load(open(spec_path, encoding="utf-8"))
    async def list_tools(ctx, params):  # noqa: ARG001
        return types.ListToolsResult(tools=[types.Tool(name=name, description=f"{name} op", input_schema={"type": "object"})
                                            for name in spec["tools"]])

    async def call_tool(ctx, params):  # noqa: ARG001
        name, arguments = params.name, params.arguments
        with open(spec["log"], "a", encoding="utf-8") as handle:
            handle.write(json.dumps([name, arguments]) + "\n")
        failure = None
        if name in spec.get("raises", []):
            failure = "server went away"
        else:
            result = (spec.get("results") or {}).get(name) or {}
            if result.get("ok", True) is False:
                failure = result.get("error") or "failed"
        if failure:
            return types.CallToolResult(content=[types.TextContent(type="text", text=failure)], is_error=True)
        return types.CallToolResult(content=[types.TextContent(type="text", text=f"done {name}")])

    server = Server("write-through-mcp-real", on_list_tools=list_tools, on_call_tool=call_tool)

    async def run():
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())
    asyncio.run(run())


if __name__ == "__main__":
    main(sys.argv[1])
