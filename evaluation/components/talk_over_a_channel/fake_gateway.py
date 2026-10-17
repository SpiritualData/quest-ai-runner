"""The fake MCP client for the talk_over_a_channel component: the process edge, nothing else."""
import json

from quest_ai_runner.adapters.mcp_client import MCPToolResult

ALLOWED = {"events_wait", "messages_send", "attachments_fetch"}


class FakeGateway:
    def __init__(self, args):
        raw = args.get("content")
        self.content = raw if isinstance(raw, str) else json.dumps(raw if raw is not None else {"events": []})
        self.error = args.get("error")
        self.raises = bool(args.get("raises"))
        self.attachment = args.get("attachment")
        self.calls = []

    def call_tool(self, name, args=None, *, timeout=None):  # noqa: ARG002
        self.calls.append([name, args])
        if self.raises:
            raise RuntimeError("gateway process died")
        if name == "attachments_fetch":
            return MCPToolResult(ok=True, content=json.dumps(self.attachment or {}))
        if self.error:
            return MCPToolResult(ok=False, error=self.error)
        return MCPToolResult(ok=True, content=self.content)

    def close(self):
        if self.raises:
            raise RuntimeError("close failed")

    def only_allowed(self):
        return all(c[0] in ALLOWED for c in self.calls)
