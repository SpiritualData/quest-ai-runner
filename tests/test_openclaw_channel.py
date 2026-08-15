"""Offline tests for ``adapters/openclaw_channel.py``.

Fully offline: no real ``openclaw`` binary, no subprocess, no network. ``OpenClawChannel`` wraps
``MCPClient``, and the constructor accepts a pre-built client (dependency injection) -- these tests
inject a scripted ``FakeMCPClient`` that satisfies the same ``call_tool``/``close`` surface
``MCPClient`` exposes, exactly the "fake at the same seam level phase 1 used" the mcp_client tests
already establish (``tests/test_mcp_client.py`` fakes one level lower, at the SDK's own session;
this fakes at the client's own public surface, which is the natural injection seam for a module
that WRAPS ``MCPClient`` rather than talking to the SDK directly).

Pinned here:
  * receive() translates a well-formed events_wait payload into InboundMessage(s);
  * receive() on a wire failure returns InboundBatch(error=..., healthy=False), never raises;
  * receive() on a raising fake still returns a value, never propagates;
  * send() translates OutboundReply -> messages_send args and reads back a message_id;
  * send() on a tool-level failure returns SendResult(ok=False), never raises;
  * close() is idempotent and never raises even if the underlying client's close() raises;
  * the bridge never calls "permissions_respond" (structurally impossible: no such method exists,
    and ALLOWED_TOOLS names only the tools this module is built to call).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import pytest

from quest_ai_runner.adapters.mcp_client import MCPToolResult
from quest_ai_runner.adapters.openclaw_channel import OpenClawChannel, OpenClawChannelConfig
from quest_ai_runner.core.adapters import OutboundReply


@dataclass
class FakeMCPClient:
    """A stand-in for ``MCPClient``'s public surface, scripted per-tool."""

    tool_results: Dict[str, Any] = field(default_factory=dict)   # tool_name -> MCPToolResult
    tool_errors: Dict[str, Exception] = field(default_factory=dict)  # tool_name -> raises
    closed: bool = False
    close_error: Optional[Exception] = None
    calls: List[tuple] = field(default_factory=list)

    def call_tool(self, name: str, args: Optional[Dict[str, Any]] = None, *,
                  timeout: Optional[float] = None) -> MCPToolResult:
        self.calls.append((name, args, timeout))
        if name in self.tool_errors:
            raise self.tool_errors[name]
        if name in self.tool_results:
            return self.tool_results[name]
        return MCPToolResult(ok=False, error=f"no script for tool {name!r}")

    def close(self) -> None:
        self.closed = True
        if self.close_error is not None:
            raise self.close_error


def channel(client: FakeMCPClient, **overrides: Any) -> OpenClawChannel:
    cfg = OpenClawChannelConfig(token_file="/fake/token", channel="openclaw", **overrides)
    return OpenClawChannel(cfg, client=client)


def events_result(events: List[Dict[str, Any]]) -> MCPToolResult:
    return MCPToolResult(ok=True, content=json.dumps({"events": events}))


# --- receive() -------------------------------------------------------------------------------

def test_receive_translates_message_events():
    fake = FakeMCPClient(tool_results={"events_wait": events_result([
        {
            "type": "message", "message_id": "m1", "conversation_id": "chat-1",
            "sender_id": "alice", "sender_label": "Alice", "text": "hello there",
            "received_at": "2026-08-14T12:00:00Z",
        },
        {
            "type": "message", "message_id": "m2", "conversation_id": "chat-1",
            "sender_id": "bob", "text": "hi",
        },
    ])})
    ch = channel(fake)
    batch = ch.receive(timeout=10)
    assert batch.error is None
    assert batch.healthy is True
    assert len(batch.messages) == 2
    m1 = batch.messages[0]
    assert m1.channel == "openclaw"
    assert m1.message_id == "m1"
    assert m1.chat_ref == "chat-1"
    assert m1.sender_id == "alice"
    assert m1.sender_label == "Alice"
    assert m1.text == "hello there"
    assert m1.attachments == []
    # events_wait was called with a clamped, non-negative timeout.
    name, args, timeout = fake.calls[0]
    assert name == "events_wait"
    assert args["timeout"] == 10
    assert timeout > 10  # headroom over the server's own window


def test_receive_clamps_timeout_to_openclaw_max():
    fake = FakeMCPClient(tool_results={"events_wait": events_result([])})
    ch = channel(fake)
    ch.receive(timeout=10_000)
    _, args, _ = fake.calls[0]
    assert args["timeout"] == 300  # EVENTS_WAIT_MAX_TIMEOUT


def test_receive_ignores_non_message_events():
    fake = FakeMCPClient(tool_results={"events_wait": events_result([
        {"type": "typing", "conversation_id": "chat-1"},
        {"type": "message", "message_id": "m1", "conversation_id": "chat-1", "sender_id": "a",
         "text": "hi"},
    ])})
    ch = channel(fake)
    batch = ch.receive()
    assert len(batch.messages) == 1
    assert batch.messages[0].message_id == "m1"


def test_receive_drops_malformed_event_without_raising():
    fake = FakeMCPClient(tool_results={"events_wait": events_result([
        {"type": "message", "text": "no ids here"},   # missing message_id/conversation_id
    ])})
    ch = channel(fake)
    batch = ch.receive()
    assert batch.messages == []
    assert batch.error is None  # a clean call that yielded nothing usable, not a wire failure


def test_receive_clean_timeout_is_not_an_error():
    """A well-formed empty events list (the ordinary long-poll-timed-out-with-nothing-new case)
    must NOT be reported as an error."""
    fake = FakeMCPClient(tool_results={"events_wait": events_result([])})
    ch = channel(fake)
    batch = ch.receive()
    assert batch.messages == []
    assert batch.error is None
    assert batch.healthy is True


def test_receive_tool_failure_returns_unhealthy_batch():
    fake = FakeMCPClient(tool_results={
        "events_wait": MCPToolResult(ok=False, error="subprocess died"),
    })
    ch = channel(fake)
    batch = ch.receive()
    assert batch.error == "subprocess died"
    assert batch.healthy is False
    assert batch.messages == []


def test_receive_never_raises_even_if_client_raises():
    """MCPClient itself never raises per its OWN contract, but OpenClawChannel adds its own
    backstop too (defense in depth against any object satisfying the client surface, including a
    future implementation or a test double) -- a raising injected client must NOT propagate."""
    fake = FakeMCPClient(tool_errors={"events_wait": RuntimeError("boom")})
    ch = channel(fake)
    batch = ch.receive()
    assert batch.messages == []
    assert batch.healthy is False
    assert "boom" in (batch.error or "")


def test_send_never_raises_even_if_client_raises():
    fake = FakeMCPClient(tool_errors={"messages_send": RuntimeError("boom")})
    ch = channel(fake)
    result = ch.send(OutboundReply(chat_ref="chat-1", text="hi"))
    assert result.ok is False
    assert "boom" in (result.error or "")


# --- attachments -------------------------------------------------------------------------------

def test_receive_fetches_attachment_bytes():
    fake = FakeMCPClient(tool_results={
        "events_wait": events_result([{
            "type": "message", "message_id": "m1", "conversation_id": "chat-1", "sender_id": "a",
            "text": "see attached",
            "attachments": [{"attachment_id": "att-1", "filename": "pic.png",
                             "mime_type": "image/png"}],
        }]),
        "attachments_fetch": MCPToolResult(
            ok=True, content=json.dumps({"data_base64": "aGVsbG8="})),  # b"hello"
    })
    ch = channel(fake)
    batch = ch.receive()
    att = batch.messages[0].attachments[0]
    assert att["filename"] == "pic.png"
    assert att["mime_type"] == "image/png"
    assert att["kind"] == "image"
    assert att["data"] == b"hello"


def test_receive_attachment_fetch_failure_degrades_to_empty_data():
    fake = FakeMCPClient(tool_results={
        "events_wait": events_result([{
            "type": "message", "message_id": "m1", "conversation_id": "chat-1", "sender_id": "a",
            "text": "see attached",
            "attachments": [{"attachment_id": "att-1", "filename": "doc.pdf"}],
        }]),
        "attachments_fetch": MCPToolResult(ok=False, error="not found"),
    })
    ch = channel(fake, fetch_attachments=True)
    batch = ch.receive()
    att = batch.messages[0].attachments[0]
    assert att["data"] == b""
    assert att["filename"] == "doc.pdf"


def test_fetch_attachments_disabled_skips_the_call():
    fake = FakeMCPClient(tool_results={
        "events_wait": events_result([{
            "type": "message", "message_id": "m1", "conversation_id": "chat-1", "sender_id": "a",
            "attachments": [{"attachment_id": "att-1", "filename": "doc.pdf"}],
        }]),
    })
    ch = channel(fake, fetch_attachments=False)
    batch = ch.receive()
    assert batch.messages[0].attachments[0]["data"] == b""
    assert all(name != "attachments_fetch" for name, _a, _t in fake.calls)


# --- send() ----------------------------------------------------------------------------------

def test_send_success_returns_message_id():
    fake = FakeMCPClient(tool_results={
        "messages_send": MCPToolResult(ok=True, content=json.dumps({"message_id": "out-1"})),
    })
    ch = channel(fake)
    result = ch.send(OutboundReply(chat_ref="chat-1", text="hi back", reply_to_message_id="m1"))
    assert result.ok is True
    assert result.message_id == "out-1"
    name, args, _ = fake.calls[0]
    assert name == "messages_send"
    assert args["conversation_id"] == "chat-1"
    assert args["text"] == "hi back"
    assert args["reply_to_message_id"] == "m1"


def test_send_without_message_id_in_response_still_ok():
    fake = FakeMCPClient(tool_results={"messages_send": MCPToolResult(ok=True, content="")})
    ch = channel(fake)
    result = ch.send(OutboundReply(chat_ref="chat-1", text="hi"))
    assert result.ok is True
    assert result.message_id is None


def test_send_tool_failure_returns_ok_false():
    fake = FakeMCPClient(tool_results={
        "messages_send": MCPToolResult(ok=False, error="rate limited"),
    })
    ch = channel(fake)
    result = ch.send(OutboundReply(chat_ref="chat-1", text="hi"))
    assert result.ok is False
    assert result.error == "rate limited"


# --- close() -----------------------------------------------------------------------------------

def test_close_is_idempotent_and_never_raises():
    fake = FakeMCPClient(close_error=RuntimeError("teardown exploded"))
    ch = channel(fake)
    ch.close()
    ch.close()  # idempotent
    assert fake.closed is True


# --- the permission boundary -------------------------------------------------------------------

def test_bridge_never_calls_permissions_respond():
    """Structural proof, not a scan: OpenClawChannel names it only in documentation (the module
    docstring explains the boundary) -- ALLOWED_TOOLS (the documented call boundary) does not
    include it, and it never appears as a tool-name argument passed to call_tool()."""
    from quest_ai_runner.adapters import openclaw_channel as oc

    assert "permissions_respond" not in oc.ALLOWED_TOOLS
    source = open(oc.__file__).read()
    # It appears in prose (the docstring explaining the boundary) -- that's fine and expected.
    # What must NOT exist is a call site: "call_tool(" followed by that tool name as an argument.
    assert 'call_tool("permissions_respond"' not in source
    assert "call_tool('permissions_respond'" not in source
