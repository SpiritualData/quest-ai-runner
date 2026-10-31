"""REAL GATEWAY (the single-case arm of ``talk-over-a-channel``): the production ``OpenClawChannel`` with NO injected client, so it builds
its own ``MCPClient`` and spawns the gateway command as a child process, exactly as production spawns ``openclaw mcp serve``. The
command is a real MCP server process (``talk_over_a_channel/real_gateway.py``, built on the MCP SDK). Receive, send, the clamped
long-poll window, attachment fetches, a gateway that answers with an error and a gateway process that dies mid-call all cross a real
pipe, and the tools called are counted by the gateway process itself. Zero model calls, zero network (a child process on this host)."""
import json
import os
import sys
import tempfile
from pathlib import Path

from quest_ai_runner.adapters.openclaw_channel import OpenClawChannel, OpenClawChannelConfig
from quest_ai_runner.core.adapters_channels import OutboundReply

GATEWAY = Path(__file__).resolve().parent.parent / "real_gateway.py"
ALLOWED = {"events_wait", "messages_send", "attachments_fetch"}


def build(meter):  # noqa: ARG001
    def talk(kind, args):
        with tempfile.TemporaryDirectory() as folder:
            log = os.path.join(folder, "calls.jsonl")
            spec_file = os.path.join(folder, "spec.json")
            token = os.path.join(folder, "token")
            Path(token).write_text("not-a-real-token", encoding="utf-8")
            Path(spec_file).write_text(json.dumps({key: args.get(key) for key in ("content", "error", "attachment", "raises")}
                                                  | {"log": log}), encoding="utf-8")
            config = OpenClawChannelConfig(token_file=token, command=sys.executable, args=[str(GATEWAY), spec_file], call_timeout_s=20.0,
                                           channel="openclaw", fetch_attachments=bool(args.get("fetch_attachments")))
            channel = OpenClawChannel(config)

            def calls():
                if not os.path.exists(log):
                    return []
                return [json.loads(line) for line in Path(log).read_text(encoding="utf-8").splitlines() if line.strip()]
            try:
                if kind == "receive":
                    batch = channel.receive(timeout=args.get("timeout", 25.0))
                    seen = calls()
                    wait = next((c[1]["timeout"] for c in seen if c[0] == "events_wait"), None)
                    return {"ids": [m.message_id for m in batch.messages], "chats": [m.chat_ref for m in batch.messages],
                            "texts": [m.text for m in batch.messages], "healthy": batch.healthy, "error": bool(batch.error),
                            "wait": wait, "att_bytes": [len(a["data"]) for m in batch.messages for a in m.attachments],
                            "only_allowed": all(c[0] in ALLOWED for c in seen)}
                if kind == "send":
                    res = channel.send(OutboundReply(chat_ref=args["chat_ref"], text=args["text"], reply_to_message_id=args.get("reply_to")))
                    seen = calls()
                    sent = next((c for c in seen if c[0] == "messages_send"), [None, None])
                    return {"ok": res.ok, "message_id": res.message_id, "tool": sent[0], "sent": sent[1],
                            "only_allowed": all(c[0] in ALLOWED for c in seen)}
                if kind == "close":
                    channel.receive(timeout=1.0)   # touch the gateway first so there is a live (or dead) process to close
                    channel.close()
                    return {"survived": True}
                raise ValueError(kind)
            finally:
                channel.close()
    return talk
