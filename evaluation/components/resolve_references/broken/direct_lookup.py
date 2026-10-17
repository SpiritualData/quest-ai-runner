"""BASELINE: look the resolver up by type and call it. A type with no resolver is skipped silently, an empty
result is dropped, a resolver error escapes, there is no character budget or item cap, no abridged-summary
marker, and any item may be a pointer. The floor the production registry and renderer must beat. Zero
model calls."""
from resolve_references.impls.card_resolvers import scripted


def build(meter):  # noqa: ARG001
    def resolve(kind, args):
        if kind != "render":
            raise ValueError(kind)
        files = args.get("files") or {}
        registry = {t: scripted(s) for t, s in (args.get("consumer") or {}).items()}
        out = []
        for item in sorted(args["items"], key=lambda i: -(i.get("ts") or 0)):
            loc = item.get("locator", {})
            if item["type"] == "note" and "note" not in registry:
                text = loc.get("text", "")
            elif item["type"] == "file" and "file" not in registry:
                text = files.get(loc.get("path"), "")
            elif item["type"] in registry:
                text = registry[item["type"]].resolve(loc)
            else:
                continue
            if text:
                out.append((item["type"], text))
        return {"n": len(out), "types": [t for t, _ in out], "text": "\n".join(x for _, x in out),
                "pointer": [True for _ in out]}
    return resolve
