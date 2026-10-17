"""CURRENT: the production ``MCPWriteAdapter`` (``adapters/mcp_write_adapter.py``) with a fake MCP client
injected at the process edge. Zero model calls, zero network."""
from quest_ai_runner.adapters.mcp_client import MCPServerSpec
from quest_ai_runner.adapters.mcp_write_adapter import MCPWriteAdapter
from write_through_mcp.fake_mcp import FakeMCPClient


def build(meter):  # noqa: ARG001
    def mcp_write(kind, args):
        client = FakeMCPClient(args)
        adapter = MCPWriteAdapter(alias=args.get("alias", "srv"), writable_tools=args.get("writable", []),
                                  spec=MCPServerSpec(alias="srv", transport="stdio", command="fake", timeout_s=5.0),
                                  client=client)
        if kind == "list":
            return {"names": [op["name"] for op in adapter.list_writable_operations()]}
        if kind == "write":
            name, targs = args["call"]
            res = adapter.write_operation(name, targs)
            detail = res.detail or {}
            return {"ok": res.ok, "n_calls": len(client.calls), "calls": client.calls,
                    "detail_tool": detail.get("tool"), "error": bool(res.error)}
        raise ValueError(kind)
    return mcp_write
