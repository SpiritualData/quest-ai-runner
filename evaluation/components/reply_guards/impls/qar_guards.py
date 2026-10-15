"""CURRENT: the production engines, called directly. ``LinkGuard.sanitize`` with a fake HTTP probe (the one
network seam), ``link_detect.links_in_message``, an in-memory ``RecipeStore``, and the pure anticipation
functions (``reinforce_or_create``, ``chips_for_now``, ``match_actual``, ``update_weight``). Zero model
calls, zero network."""
from datetime import datetime

from quest_ai_runner.core import anticipation as ant
from quest_ai_runner.core.link_detect import links_in_message
from quest_ai_runner.core.link_guard import LinkGuard, LinkPolicy
from quest_ai_runner.core.recipes import RecipeStore


def build(meter):  # noqa: ARG001
    def guard(kind, args):
        if kind == "link_sanitize":
            table = args.get("fetch") or {}

            def fetch(url, timeout):  # noqa: ARG001
                status, detail = table.get(url, [None, "timeout"])
                return status, detail
            policy = LinkPolicy.from_dict(dict(args["policy"], passes=1))
            text, verdicts = LinkGuard(policy, fetcher=fetch).sanitize(args["text"])
            return {"text": text, "verdicts": {v.url: v.verdict for v in verdicts}}
        if kind == "link_extract":
            pairs = links_in_message(args["text"])
            return {"urls": [u for u, _ in pairs], "labels": [lab for _, lab in pairs]}
        if kind in ("recipe_match", "recipe_learn"):
            store = RecipeStore()
            for request, tool, targs in args["learned"]:
                store.learn(request, tool, targs, scope_tags=args.get("learn_scope"))
            if kind == "recipe_learn":
                return {"recipes": len(store.all())}
            found = store.match(args["request"], scope_tags=args.get("scope"))
            return {"tool": found[0][0].tool if found else None, "n": len(found)}
        if kind == "anticipation_chips":
            scope, patterns = "conv:eval", []
            for text, when in args["asks"]:
                at = datetime.fromisoformat(when)
                patterns, _ = ant.reinforce_or_create(patterns, ant.extract_features(text, at, scope), text,
                                                      at.timestamp())
            chips = ant.chips_for_now(patterns, datetime.fromisoformat(args["now"]), args.get("recent") or [])
            texts = [c.text for c in chips]
            return {"top": texts[0] if texts else None, "texts": texts, "n": len(texts), "patterns": len(patterns)}
        if kind == "anticipation_match":
            live = [ant.Prediction(prediction_id=f"p{i}", text=t, confidence=0.6) for i, t in enumerate(args["predictions"])]
            best, _score, _ = ant.match_actual(live, args["actual"])
            return {"served": best.text if best else None}
        if kind == "anticipation_weight":
            w = args["w"]
            for s in args["scores"]:
                w = ant.update_weight(w, s)
            return {"w": w, "pruned": w < ant.PRUNE_WEIGHT}
        raise ValueError(kind)
    return guard
