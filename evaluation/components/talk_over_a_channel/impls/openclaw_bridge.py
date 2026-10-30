"""CURRENT: the production ``OpenClawChannel`` (``adapters/openclaw_channel.py``) with a fake MCP client
injected at the process edge. Zero model calls, zero network."""
import base64

from quest_ai_runner.adapters.openclaw_channel import OpenClawChannel, OpenClawChannelConfig
from quest_ai_runner.core.adapters_channels import OutboundReply
from talk_over_a_channel.fake_gateway import FakeGateway


def build(meter):  # noqa: ARG001
    def talk(kind, args):
        gw = FakeGateway(args)
        channel = OpenClawChannel(OpenClawChannelConfig(token_file="/fake/token", channel="openclaw",
                                                         fetch_attachments=bool(args.get("fetch_attachments"))), client=gw)
        if kind == "receive":
            batch = channel.receive(timeout=args.get("timeout", 25.0))
            wait = next((c[1]["timeout"] for c in gw.calls if c[0] == "events_wait"), None)
            return {"ids": [m.message_id for m in batch.messages], "chats": [m.chat_ref for m in batch.messages],
                    "texts": [m.text for m in batch.messages], "healthy": batch.healthy, "error": bool(batch.error),
                    "wait": wait, "att_bytes": [len(a["data"]) for m in batch.messages for a in m.attachments],
                    "only_allowed": gw.only_allowed()}
        if kind == "send":
            res = channel.send(OutboundReply(chat_ref=args["chat_ref"], text=args["text"],
                                             reply_to_message_id=args.get("reply_to")))
            sent = next((c for c in gw.calls if c[0] == "messages_send"), [None, None])
            return {"ok": res.ok, "message_id": res.message_id, "tool": sent[0], "sent": sent[1],
                    "only_allowed": gw.only_allowed()}
        if kind == "close":
            channel.close()
            return {"survived": True}
        raise ValueError(kind)
    return talk
