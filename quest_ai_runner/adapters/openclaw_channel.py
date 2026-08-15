"""OpenClawChannel -- a ``ChannelTransport`` over OpenClaw's MCP server.

OpenClaw (github.com/openclaw/openclaw, MIT) is an existing, maintained open-source personal-AI
gateway with working connectors for WhatsApp/Telegram/Discord/Slack/Google Chat/Signal, all
exposed as ONE MCP server via ``openclaw mcp serve``. This module is a HUB-TO-HUB bridge: it wraps
the generic ``MCPClient`` (``adapters/mcp_client.py``) to speak that MCP surface, so every channel
OpenClaw has configured becomes a live QAR channel with no new QAR code -- a new channel is
OpenClaw-side config, not a new adapter.

It knows exactly six OpenClaw MCP tools, and DELIBERATELY never calls a seventh:

  * ``events_wait``    -- long-poll for new events (default 30s, max 300s; an in-memory live
                           event queue on OpenClaw's side). Drives ``receive()``.
  * ``messages_send``  -- send a reply. Drives ``send()``.
  * ``conversations_list`` / ``conversation_get`` / ``messages_read`` / ``attachments_fetch`` --
                           available on the server; this module only calls ``attachments_fetch``
                           (best-effort, to hydrate an inbound message's attachment bytes) and
                           does not otherwise poll history (``events_wait`` already delivers new
                           messages as they arrive).
  * ``permissions_list_open`` / ``permissions_respond`` -- OpenClaw's own approval surface. This
    bridge NEVER calls ``permissions_respond``: approvals are QAR/Quest's job (Escalation /
    EVENT_DECISION), never the gateway's. There is no method on this class that could call it.

SECURITY -- non-negotiable lockdown requirements for how OpenClaw ITSELF must be run (this module
does not install or operate OpenClaw; see ``docs/live-channels.md`` for the full checklist an
operator must verify before pointing this bridge at a real Gateway):

  * OpenClaw pinned at or above v2026.1.29 (fixes CVE-2026-25253, CVSS 8.8: auth-token
    exfiltration via a spoofed ``gatewayUrl`` leading to RCE, ~40k exposed instances affected).
  * Skills/plugins/cron/browser-automation all DISABLED in OpenClaw's own config, and NO
    agent/model configured there at all -- OpenClaw is pure message relay; QAR is the brain.
  * OpenClaw's Gateway bound to localhost only, never a public/remote URL.
  * OpenClaw never holds ``QUEST_API_KEY``, any model key, or corpus access -- only its own
    channel bot credentials and its own Gateway token.

AUTH: only a Gateway TOKEN FILE PATH, injected via ``OpenClawChannelConfig.token_file`` -- never
hardcoded (same injection discipline as ``google_chat_adapter.py``'s ``TokenProvider``: the
consumer supplies where the credential lives, this module never mints or stores one itself). The
token file is handed to the ``openclaw mcp serve`` subprocess via ``--token-file``, so the token
never has to pass through this process's own environment or memory as a bare string.

NEVER raises. Every failure -- a missing ``openclaw`` binary, a dead subprocess, a malformed
event payload, a call timeout -- comes back as ``InboundBatch(error=..., healthy=False)`` or
``SendResult(ok=False, error=...)``, exactly like every other adapter in this repo.

The exact JSON shape OpenClaw's ``events_wait``/``messages_send``/``attachments_fetch`` tools
return is not pinned to a fixture in this codebase (no live OpenClaw instance is available at
authoring time), so parsing here is DELIBERATELY tolerant: it accepts a handful of plausible key
spellings per field (``conversation_id``/``chat_id``/``chat_ref``, ``sender_id``/``from``, ...)
the same way ``mcp_client.field_of``/``acp_deep_runner.field_of`` tolerate SDK version drift, and
degrades to a clearly-logged skip rather than raising or fabricating a message on a payload shape
it does not recognize. Adjust ``_TOOL_ARGS_*`` / ``_extract_events`` if the real server's schema
differs once this is wired against one.
"""
from __future__ import annotations

import base64
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ..core.adapters import (
    ChannelTransportBase,
    InboundBatch,
    InboundMessage,
    OutboundReply,
    SendResult,
)
from .mcp_client import MCPClient, MCPServerSpec, MCPToolResult

_log = logging.getLogger("quest-ai-runner.openclaw-channel")

# OpenClaw's own documented bound on events_wait's long-poll window (confirmed tool contract:
# default 30s, max 300s). Clamped here so a caller's ``timeout`` can never ask the gateway to
# hold a connection open longer than IT allows.
EVENTS_WAIT_DEFAULT_TIMEOUT = 30.0
EVENTS_WAIT_MAX_TIMEOUT = 300.0

# The tool names this bridge is allowed to call. Deliberately excludes "permissions_respond" --
# see the module docstring. Kept as a named constant (rather than only "just not calling it") so
# a future maintainer adding a method here has one place that states the boundary explicitly.
ALLOWED_TOOLS = (
    "events_wait",
    "messages_send",
    "attachments_fetch",
)


def _first(d: Dict[str, Any], *keys: str, default: Any = None) -> Any:
    """First present, non-None key among ``keys``, else ``default``. Never raises."""
    for k in keys:
        v = d.get(k)
        if v is not None:
            return v
    return default


def _as_str(v: Any) -> str:
    if v is None:
        return ""
    return str(v)


@dataclass
class OpenClawChannelConfig:
    """Connection + policy for ONE OpenClaw MCP server. Consumer-supplied, nothing baked in."""

    # Path to the Gateway token file -- REQUIRED, injected by the consumer (never hardcoded; see
    # the module docstring). Passed to the subprocess as ``--token-file <path>``.
    token_file: str = ""
    # The command that launches the OpenClaw MCP server. Default assumes ``openclaw`` is on the
    # PATH the subprocess will see (see ``env_allowlist``).
    command: str = "openclaw"
    args: List[str] = field(default_factory=lambda: ["mcp", "serve"])
    # The child's ENTIRE environment (never the parent's; see ``mcp_client.build_stdio_env``).
    # Typically at minimum a PATH entry so ``openclaw`` resolves -- ``MCPClient``/``build_stdio_env``
    # adds the parent's PATH automatically when this omits one.
    env_allowlist: Optional[Dict[str, str]] = None
    cwd: Optional[str] = None
    # Per-call timeout for calls OTHER than events_wait (which uses its own ``receive(timeout=)``
    # argument, clamped to OpenClaw's documented 30s/300s window).
    call_timeout_s: float = 30.0
    # Best-effort: hydrate each inbound attachment's bytes via ``attachments_fetch`` before
    # handing the message to the runner. False leaves attachments as metadata-only references
    # (filename/mime_type, empty ``data``) -- still usable, just not auto-downloaded.
    fetch_attachments: bool = True
    # The channel name this transport reports (``InboundMessage.channel`` /
    # ``ChannelTransport.channel``, and the dedup key prefix a ``ChannelRunner`` uses). OpenClaw
    # itself may proxy several underlying channels (WhatsApp, Telegram, ...); each individual
    # inbound event's own channel (if the payload carries one) is preserved on
    # ``InboundMessage.raw`` for a consumer that wants to branch on it, but the TRANSPORT's own
    # identity -- what a ChannelRunner dedups and labels by -- is this single name.
    channel: str = "openclaw"


class OpenClawChannel(ChannelTransportBase):
    """``ChannelTransport`` over OpenClaw's MCP server. Wraps ``MCPClient`` -- never reimplements
    subprocess/stdio handling.
    """

    def __init__(self, config: OpenClawChannelConfig, *, client: Optional[MCPClient] = None):
        self.cfg = config
        self.channel = config.channel
        # Dependency-injected for tests (see tests/test_openclaw_channel.py): a caller may hand in
        # any object satisfying MCPClient's public surface (call_tool/close). Production callers
        # omit ``client`` and get a real one built from ``config``.
        self._client = client if client is not None else MCPClient(self._build_spec(config))

    @staticmethod
    def _build_spec(config: OpenClawChannelConfig) -> MCPServerSpec:
        if not config.token_file:
            _log.warning(
                "OpenClawChannelConfig.token_file is empty -- the spawned 'openclaw mcp serve' "
                "will run with no --token-file argument, which is very likely to fail its own "
                "auth. Set token_file to the Gateway token file path.")
        args = list(config.args or [])
        if config.token_file:
            args = args + ["--token-file", config.token_file]
        return MCPServerSpec(
            alias=config.channel,
            transport="stdio",
            command=config.command,
            args=args,
            env_allowlist=config.env_allowlist,
            cwd=config.cwd,
            timeout_s=config.call_timeout_s,
        )

    # --- ChannelTransport ------------------------------------------------------------------

    def _call_tool(self, name: str, args: Dict[str, Any], *, timeout: float) -> Any:
        """``self._client.call_tool`` with an extra never-raise backstop.

        ``MCPClient.call_tool`` is documented to never raise, but this bridge is also unit-tested
        AND designed to be safe against any object satisfying its surface (including a test double
        or a future client implementation) -- so this is defense in depth, not a workaround for a
        known bug. Returns an ``ok=False`` ``MCPToolResult``-shaped value on any exception.
        """
        try:
            return self._client.call_tool(name, args, timeout=timeout)
        except Exception as e:  # noqa: BLE001 — the ONE backstop; see docstring
            _log.warning("openclaw call_tool(%s) raised (should never happen): %s: %s",
                        name, type(e).__name__, e)
            return MCPToolResult(ok=False, error=f"{type(e).__name__}: {e}")

    def receive(self, *, timeout: float = 25.0) -> InboundBatch:
        """Long-poll ``events_wait`` and translate any message-type events. Never raises."""
        wait_seconds = max(0.0, min(float(timeout or EVENTS_WAIT_DEFAULT_TIMEOUT),
                                    EVENTS_WAIT_MAX_TIMEOUT))
        result = self._call_tool(
            "events_wait", {"timeout": wait_seconds},
            # Give the wire call some headroom over the server's own long-poll window so a call
            # that legitimately takes ~wait_seconds is never mistaken for a client-side timeout.
            timeout=wait_seconds + 15.0,
        )
        if not result.ok:
            return InboundBatch(error=result.error or "events_wait failed", healthy=False)
        events = _parse_json_list(result.content, key_candidates=("events", "results", "items"))
        if events is None:
            # Nothing parseable -- treat as a clean empty result rather than an error: an
            # events_wait timeout with literally nothing to say is the common, expected outcome.
            return InboundBatch()
        messages: List[InboundMessage] = []
        for ev in events:
            if not isinstance(ev, dict):
                continue
            msg = self._message_from_event(ev)
            if msg is not None:
                messages.append(msg)
        return InboundBatch(messages=messages)

    def send(self, reply: OutboundReply) -> SendResult:
        """Send one reply via ``messages_send``. Never raises."""
        args: Dict[str, Any] = {
            "conversation_id": reply.chat_ref,
            "text": reply.text,
        }
        if reply.reply_to_message_id:
            args["reply_to_message_id"] = reply.reply_to_message_id
        result = self._call_tool("messages_send", args, timeout=self.cfg.call_timeout_s)
        if not result.ok:
            return SendResult(ok=False, error=result.error or "messages_send failed")
        message_id = None
        payload = _parse_json_obj(result.content)
        if payload is not None:
            message_id = _first(payload, "message_id", "id", "messageId")
        return SendResult(ok=True, message_id=_as_str(message_id) if message_id else None)

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:  # noqa: BLE001 — close is idempotent and must never raise
            _log.debug("openclaw channel close raised (ignored)", exc_info=True)

    # --- translation -------------------------------------------------------------------------

    def _message_from_event(self, ev: Dict[str, Any]) -> Optional[InboundMessage]:
        """One event dict -> ``InboundMessage``, or ``None`` if it is not a message-type event
        (or is unusably malformed). Tolerant field access; never raises -- see module docstring.
        """
        kind = _as_str(_first(ev, "type", "kind", "event_type")).strip().lower()
        # A structural field on the OpenClaw payload, not text the model generated -- filtering on
        # it is ordinary protocol parsing, not the keyword-gated control flow hard rule #3 forbids.
        if kind and kind not in ("message", "message.received", "message_received"):
            return None
        message_id = _as_str(_first(ev, "message_id", "id", "messageId")).strip()
        chat_ref = _as_str(_first(ev, "conversation_id", "chat_id", "chat_ref",
                                  "conversationId", "chatId")).strip()
        sender_id = _as_str(_first(ev, "sender_id", "from", "senderId", "user_id")).strip()
        if not message_id or not chat_ref:
            _log.warning("openclaw event missing message_id/conversation_id; dropping: %r",
                         {k: ev.get(k) for k in list(ev)[:6]})
            return None
        text = _as_str(_first(ev, "text", "body", "message", default=""))
        attachments = self._attachments_from_event(ev)
        return InboundMessage(
            channel=self.channel,
            message_id=message_id,
            chat_ref=chat_ref,
            sender_id=sender_id,
            sender_label=_as_str(_first(ev, "sender_label", "sender_name", "senderLabel")),
            text=text,
            attachments=attachments,
            received_at=_as_str(_first(ev, "received_at", "timestamp", "receivedAt")),
            raw=ev,
        )

    def _attachments_from_event(self, ev: Dict[str, Any]) -> List[Dict[str, Any]]:
        raw_atts = ev.get("attachments")
        if not isinstance(raw_atts, list) or not raw_atts:
            return []
        out: List[Dict[str, Any]] = []
        for raw in raw_atts:
            if not isinstance(raw, dict):
                continue
            filename = _as_str(_first(raw, "filename", "name", default="attachment"))
            mime_type = _as_str(_first(raw, "mime_type", "mimeType", "content_type", default=""))
            att_id = _first(raw, "attachment_id", "id", "attachmentId")
            item: Dict[str, Any] = {
                "filename": filename or "attachment",
                "mime_type": mime_type or "application/octet-stream",
                "data": b"",
                "kind": "image" if (mime_type or "").startswith("image/") else "file",
            }
            if self.cfg.fetch_attachments and att_id:
                data = self._fetch_attachment(att_id)
                if data:
                    item["data"] = data
            out.append(item)
        return out

    def _fetch_attachment(self, attachment_id: Any) -> bytes:
        """Best-effort bytes for one attachment via ``attachments_fetch``. Never raises; returns
        ``b""`` on any failure or an unrecognized response shape."""
        result = self._call_tool(
            "attachments_fetch", {"attachment_id": attachment_id}, timeout=self.cfg.call_timeout_s)
        if not result.ok:
            _log.info("openclaw attachments_fetch failed for %r: %s", attachment_id, result.error)
            return b""
        payload = _parse_json_obj(result.content)
        if payload is not None:
            b64 = _first(payload, "data_base64", "data", "content_base64")
            if isinstance(b64, str) and b64:
                try:
                    return base64.b64decode(b64, validate=False)
                except Exception:  # noqa: BLE001 — a malformed base64 blob is not fatal
                    _log.info("openclaw attachment %r: base64 decode failed", attachment_id)
                    return b""
        # No recognizable structured payload -- treat the raw content as text bytes rather than
        # silently dropping it (mirrors mcp_client's own "never lose content silently" stance).
        return result.content.encode("utf-8", errors="replace") if result.content else b""


# ---------------------------------------------------------------------------
# Tolerant JSON helpers -- MCPToolResult.content is plain text; OpenClaw's tools are expected to
# return JSON in it, but a malformed/non-JSON response must degrade, never raise or crash parsing.
# ---------------------------------------------------------------------------

def _parse_json_obj(text: str) -> Optional[Dict[str, Any]]:
    if not text or not text.strip():
        return None
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _parse_json_list(text: str, *, key_candidates: tuple) -> Optional[List[Any]]:
    """A JSON array, or the first list found under one of ``key_candidates`` in a JSON object.
    ``None`` (not ``[]``) means "nothing parseable" so a caller can tell that apart from a
    genuinely empty, well-formed result."""
    if not text or not text.strip():
        return None
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in key_candidates:
            v = data.get(key)
            if isinstance(v, list):
                return v
        return []  # a well-formed object with no recognizable list key: a valid empty result
    return None
