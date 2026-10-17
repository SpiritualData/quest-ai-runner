"""BASELINE: call whatever tool is named. No write allowlist, no alias handling, no failure handling (a
server error is returned as the result, a dead connection raises), and discovery lists every tool the
server has. The floor the adapter must beat. Zero model calls."""
from write_through_mcp.fake_mcp import FakeMCPClient


def build(meter):  # noqa: ARG001
    def mcp_write(kind, args):
        client = FakeMCPClient(args)
        if kind == "list":
            return {"names": [t["name"] for t in client.list_tools()]}
        if kind == "write":
            name, targs = args["call"]
            res = client.call_tool(name, targs)
            return {"ok": res.ok, "n_calls": len(client.calls), "calls": client.calls,
                    "detail_tool": name, "error": not res.ok}
        raise ValueError(kind)
    return mcp_write
