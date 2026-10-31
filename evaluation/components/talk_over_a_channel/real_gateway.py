"""A real MCP gateway for the talk_over_a_channel component: a child process that speaks MCP over stdio and answers the three
tools the OpenClaw channel uses (``events_wait``, ``messages_send``, ``attachments_fetch``).

``python real_gateway.py <spec.json>``: the spec carries what the tools answer (``content``, ``error``, ``attachment``), whether the
process dies when called (``raises``) and the file where every call is appended as one JSON line, written by this process."""
import asyncio
import json
import os
import sys

from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

TOOLS = ("events_wait", "messages_send", "attachments_fetch")


def main(spec_path):
    spec = json.load(open(spec_path, encoding="utf-8"))

    async def list_tools(ctx, params):  # noqa: ARG001
        return types.ListToolsResult(tools=[types.Tool(name=name, description=name, input_schema={"type": "object"}) for name in TOOLS])

    async def call_tool(ctx, params):  # noqa: ARG001
        with open(spec["log"], "a", encoding="utf-8") as handle:
            handle.write(json.dumps([params.name, params.arguments]) + "\n")
        if spec.get("raises"):
            os._exit(1)   # the gateway process dies mid-call
        if params.name == "attachments_fetch":
            text = json.dumps(spec.get("attachment") or {})
        elif spec.get("error"):
            return types.CallToolResult(content=[types.TextContent(type="text", text=spec["error"])], is_error=True)
        else:
            content = spec.get("content")
            text = content if isinstance(content, str) else json.dumps(content if content is not None else {"events": []})
        return types.CallToolResult(content=[types.TextContent(type="text", text=text)])

    server = Server("talk-over-a-channel-real", on_list_tools=list_tools, on_call_tool=call_tool)

    async def run():
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())
    asyncio.run(run())


if __name__ == "__main__":
    main(sys.argv[1])
