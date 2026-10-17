"""The MCP write function. Every solution is ONE function with this signature:

    mcp_write(kind: str, args: dict) -> dict

A consumer lets the brain run a mutating operation through an MCP server. The contract: exactly the
requested mutation runs, exactly once; a tool that is not on the write allowlist is refused without
touching the server (being readable grants nothing); a server error or a dead connection comes back as a
failed result, never an exception; and discovery shows only allowlisted tools under the adapter's alias.
The one fake is the MCP client (the process edge): it records every ``call_tool`` it receives.

Kinds and the dict a solution returns:
  write  args {alias, writable, tools, call: [name, args], results, raises}
         -> {"ok", "n_calls": tool calls that reached the server, "calls": [[bare tool, args], ...],
             "detail_tool": tool recorded in the result for audit, "error": bool}
         ``results`` maps a tool to {ok, error}; ``raises`` lists tools whose call raises.
  list   args {alias, writable, tools, discovery_fails}
         -> {"names": aliased operation names offered}

An implementation module in ``impls/`` exposes ``build(meter) -> mcp_write``. No solution here calls a model.
"""
