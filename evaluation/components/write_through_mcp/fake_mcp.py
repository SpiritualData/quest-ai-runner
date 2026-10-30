"""The fake MCP client for the write_through_mcp component: the process edge, nothing else."""
from hubofhubs.act.mcp_client import MCPToolResult


class FakeMCPClient:
    def __init__(self, args):
        self.tools = [{"name": n, "description": f"{n} op", "input_schema": {}} for n in args.get("tools", [])]
        self.results = args.get("results") or {}
        self.raises = set(args.get("raises") or [])
        self.discovery_fails = bool(args.get("discovery_fails"))
        self.calls = []

    def list_tools(self):
        if self.discovery_fails:
            raise RuntimeError("connection dead")
        return list(self.tools)

    def call_tool(self, name, args=None, *, timeout=None):  # noqa: ARG002
        self.calls.append([name, args])
        if name in self.raises:
            raise RuntimeError("server went away")
        res = self.results.get(name) or {}
        return MCPToolResult(ok=res.get("ok", True), content=f"done {name}", error=res.get("error"))
