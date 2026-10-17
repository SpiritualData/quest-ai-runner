"""The channel-transport function. Every solution is ONE function with this signature:

    talk(kind: str, args: dict) -> dict

A live chat over an external channel: inbound messages arrive in order from a long-poll, each reply goes
out once. The contract: well-formed message events become inbound messages in the order received (key
spelling drift tolerated, non-message and unusable events skipped), a wire failure or dead gateway comes
back as an unhealthy empty batch or a failed send and never raises, the long-poll window is clamped to the
gateway's own ceiling, attachments are hydrated on request, and the bridge only ever calls its allowed
tools (never the gateway's own approval surface). The one fake is the MCP client (the process edge); it
returns scripted tool payloads and records every tool call.

Kinds, args and the dict a solution returns:
  receive  args {content | error | raises, timeout, fetch_attachments, attachment}
           -> {"ids", "chats", "texts", "healthy", "error", "wait", "att_bytes", "only_allowed"}
           ``content`` is the events_wait payload (object, list or raw string); ``wait`` is the timeout the
           gateway was asked to wait; ``att_bytes`` the sizes of hydrated attachments.
  send     args {chat_ref, text, reply_to, content | error | raises}
           -> {"ok", "message_id", "tool", "sent": the args handed to the gateway, "only_allowed"}
  close    args {raises} -> {"survived": true}

An implementation module in ``impls/`` exposes ``build(meter) -> talk``. No solution here calls a model.
"""
