"""BASELINE: talk to the gateway with plain calls. Reads the events list under one fixed key and turns
every entry into a message (no filtering of non-message events, no key-spelling tolerance, no clamp on the
long-poll window, no attachment hydration) and lets any gateway failure raise. The floor the channel must
beat. Zero model calls."""
import json

from talk_over_a_channel.fake_gateway import FakeGateway


def build(meter):  # noqa: ARG001
    def talk(kind, args):
        gw = FakeGateway(args)
        if kind == "receive":
            res = gw.call_tool("events_wait", {"timeout": args.get("timeout", 25.0)})
            events = json.loads(res.content)["events"]
            return {"ids": [e["message_id"] for e in events], "chats": [e["conversation_id"] for e in events],
                    "texts": [e.get("text", "") for e in events], "healthy": res.ok, "error": not res.ok,
                    "wait": args.get("timeout", 25.0), "att_bytes": [], "only_allowed": gw.only_allowed()}
        if kind == "send":
            payload = {"conversation_id": args["chat_ref"], "text": args["text"]}
            res = gw.call_tool("messages_send", payload)
            return {"ok": res.ok, "message_id": None, "tool": "messages_send", "sent": payload,
                    "only_allowed": gw.only_allowed()}
        if kind == "close":
            gw.close()
            return {"survived": True}
        raise ValueError(kind)
    return talk
