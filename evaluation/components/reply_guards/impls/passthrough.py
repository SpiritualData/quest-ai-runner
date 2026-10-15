"""BASELINE: no guard. Replies are sent as written (every link believed), links are found with a plain
``https?://\\S+`` regex, a recipe matches only an identical request, no chip is ever suggested and a
prediction is served only on an identical ask. The floor the engines must beat. Zero model calls."""
import re


def build(meter):  # noqa: ARG001
    def guard(kind, args):
        if kind == "link_sanitize":
            urls = re.findall(r"https?://\S+", args["text"])
            return {"text": args["text"], "verdicts": {u: "ok" for u in urls}}
        if kind == "link_extract":
            urls = list(dict.fromkeys(re.findall(r"https?://\S+", args["text"])))
            return {"urls": urls, "labels": ["web page"] * len(urls)}
        if kind in ("recipe_match", "recipe_learn"):
            seen = {r: t for r, t, _ in args["learned"]}
            if kind == "recipe_learn":
                return {"recipes": len(seen)}
            tool = seen.get(args["request"])
            return {"tool": tool, "n": 1 if tool else 0}
        if kind == "anticipation_chips":
            return {"top": None, "texts": [], "n": 0, "patterns": 0}
        if kind == "anticipation_match":
            return {"served": args["actual"] if args["actual"] in args["predictions"] else None}
        if kind == "anticipation_weight":
            return {"w": args["w"], "pruned": False}
        raise ValueError(kind)
    return guard
