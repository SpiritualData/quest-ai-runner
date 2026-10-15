"""The channel transport port and its message value objects.

Split out of ``adapters.py`` by definition; that module re-exports every name here.
"""
from __future__ import annotations

import abc
from typing import Any, Dict, List, Optional, Protocol, runtime_checkable
from dataclasses import dataclass, field


# ---------------------------------------------------------------------------
# ChannelTransport: a LIVE, two-way messaging channel (the sixth adapter role).
# ---------------------------------------------------------------------------
#
# Everything above this line is about ONE turn: the brain reads/plans/answers, and a
# ProgressSink decides what surfaces. ChannelTransport is a different kind of boundary — it is
# how a message gets INTO the brain and a reply gets back OUT, over some real-time messaging
# surface (a phone chat app, a Slack channel, ...) that this library does not itself speak. A
# consumer's ``runner.channel_runner.ChannelRunner`` drives one ``ChannelTransport`` in a loop:
# receive a batch, authorize + dedup, run one orchestrator turn per chat, send the reply back.
#
# Reference implementation: ``adapters.openclaw_channel.OpenClawChannel``, which wraps the
# generic ``MCPClient`` (``adapters/mcp_client.py``) to talk to OpenClaw's MCP server — one
# bridge, every channel OpenClaw has configured (WhatsApp/Telegram/Discord/Slack/...) becomes a
# live QAR channel. A consumer is free to implement ``ChannelTransport`` directly against any
# other messaging surface; the runner only depends on this Protocol.
#
# Same discipline as every other adapter role: NEVER raises. A dead process, a protocol error, a
# timeout, an unreachable gateway — all of it comes back as a returned value (``InboundBatch``
# with ``error``/``healthy=False``, or ``SendResult(ok=False, error=...)``), never an exception.

@dataclass
class InboundMessage:
    """ONE message received on a channel, normalized to a transport-agnostic shape.

    ``chat_ref`` is the channel's own conversation/thread identifier — the key a
    ``ChannelRunner`` sessions turns by (one in-flight turn per ``chat_ref`` at a time; see
    ``runner.channel_runner``). ``sender_id`` is the channel's own user identifier — the ONLY
    thing an operator's ``allowed_senders`` allowlist is checked against (a plain membership
    test, never anything derived from message text). ``attachments`` uses the SAME
    ``{filename, mime_type, data, kind}`` shape ``core.attachments.prepare_attachments`` already
    consumes, so a channel's file/image uploads flow through the existing multimodal path with
    no special casing.
    """
    channel: str
    message_id: str
    chat_ref: str
    sender_id: str
    sender_label: str = ""
    text: str = ""
    attachments: List[Dict[str, Any]] = field(default_factory=list)
    received_at: str = ""
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass
class InboundBatch:
    """The outcome of ONE ``ChannelTransport.receive()`` call. Never raised — returned.

    ``error`` set + ``healthy=False`` together mean the transport itself is in trouble (a dead
    subprocess, a protocol error) — the caller should back off before retrying. ``error`` set with
    ``healthy=True`` (the default) covers a clean, ordinary empty result (e.g. a long-poll that
    simply timed out with nothing new) that still carries an informational message; most clean
    timeouts instead just return ``InboundBatch()`` with no error at all.
    """
    messages: List[InboundMessage] = field(default_factory=list)
    error: Optional[str] = None
    healthy: bool = True


@dataclass
class OutboundReply:
    """ONE reply to send back on a channel. ``kind`` is informational (a transport MAY use it to
    pick a rendering, e.g. an ack vs. the final answer) — never a control-flow signal read off
    generated text; the caller sets it explicitly from what KIND of reply this is, not by
    inspecting the text.
    """
    chat_ref: str
    text: str
    reply_to_message_id: Optional[str] = None
    kind: str = "answer"   # "ack" | "progress" | "answer" | "decision" | "error"


@dataclass
class SendResult:
    """The outcome of ONE ``ChannelTransport.send()`` call. Mirrors ``WriteResult``'s shape:
    ``ok=False`` always means nothing was delivered; never raised — returned."""
    ok: bool
    message_id: Optional[str] = None
    error: Optional[str] = None


@runtime_checkable
class ChannelTransport(Protocol):
    """A LIVE, two-way messaging channel. NEVER raises — every failure is a returned value."""

    channel: str

    def receive(self, *, timeout: float = 25.0) -> InboundBatch:
        """Block up to ``timeout`` seconds for new inbound messages (a long-poll where the
        underlying transport supports one). A clean timeout with nothing new returns an EMPTY
        ``InboundBatch()`` (no error) — that is the normal, expected outcome of most calls, not a
        failure. Never raises."""

    def send(self, reply: OutboundReply) -> SendResult:
        """Send ONE reply. Never raises."""

    def close(self) -> None:
        """Tear down any held connection/process. Idempotent. Never raises."""


class ChannelTransportBase(abc.ABC):
    """ABC variant of ``ChannelTransport`` for implementers who prefer explicit subclassing."""

    channel: str = ""

    @abc.abstractmethod
    def receive(self, *, timeout: float = 25.0) -> InboundBatch:
        """Block up to ``timeout`` seconds for new inbound messages. Never raises."""

    @abc.abstractmethod
    def send(self, reply: OutboundReply) -> SendResult:
        """Send ONE reply. Never raises."""

    def close(self) -> None:
        """No-op default. Override for a transport that holds a connection/process."""
        return None
