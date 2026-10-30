"""CURRENT: the production resolver registry (``build_resolver_registry``, the built-in note and file
resolvers, coercion of consumer callables) and the production renderer ``render_card_content_blocks``
(``hubofhubs.recall.reference_resolver``, ``hubofhubs.recall.card_content_render``), reached through the registry QAR exports as
``quest_ai_runner.adapters.build_resolver_registry``. The fakes are the consumer's
file text and scripted resolvers. Zero model calls, zero network."""
from hubofhubs.recall.card_content_render import render_card_content_blocks
from quest_ai_runner.adapters import build_resolver_registry


def scripted(spec):
    def fn(locator, max_chars=2000):  # noqa: ARG001
        if spec["mode"] == "raise":
            raise RuntimeError("data layer down")
        return "" if spec["mode"] == "empty" else spec["text"]
    if spec.get("style") == "callable":
        return fn

    class Resolver:
        def resolve(self, locator, *, max_chars=2000):
            return fn(locator, max_chars=max_chars)
    return Resolver()


def build(meter):  # noqa: ARG001
    def resolve(kind, args):
        if kind != "render":
            raise ValueError(kind)
        files = args.get("files") or {}
        registry = build_resolver_registry(file_read_text=lambda path, limit: files.get(path, "")[:limit],
                                           consumer_resolvers={t: scripted(s) for t, s in (args.get("consumer") or {}).items()})
        kw = {"max_ref_chars": args["max_ref_chars"]} if "max_ref_chars" in args else {}
        blocks = render_card_content_blocks({"content": args["items"]}, registry, task_kws=set(), **kw)
        return {"n": len(blocks), "types": [b["type"] for b in blocks], "text": "\n".join(b["text"] for b in blocks),
                "pointer": [b["pointer_eligible"] for b in blocks]}
    return resolve
