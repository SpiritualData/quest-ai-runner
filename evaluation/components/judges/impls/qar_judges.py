"""CURRENT: the production deterministic halves of the four judges, driven with a scripted provider.
``oversee`` is called for real (prompt, tool schema, parsing, degrade-to-proceed), the reach judge's
``normalize_verdict`` / ``verdict_block``, the quest judge's ``rank_quests`` and the sufficiency gate's
``collect_abridged_items`` / ``unfetched_abridged`` are the live functions. Zero model calls."""
from hubofhubs.critique import sufficiency
from quest_ai_runner.core import overseer, quest_judge, reach_judge


class ScriptedProvider:
    """A ModelProvider whose ``plan`` returns a fixed object, or raises when told to."""

    def __init__(self, raw):
        self.raw = raw

    def plan(self, prompt, *, model, tool_schema, layers=None):  # noqa: ARG002
        if self.raw == "__raise__":
            raise RuntimeError("provider down")
        return self.raw


def build(meter):  # noqa: ARG001
    def judge(kind, args):
        if kind == "overseer_parse":
            sig = overseer.oversee(ScriptedProvider(args["raw"]), "m", "digest")
            return dict(signal=sig.signal, hint=sig.hint, reason=sig.reason, degraded=sig.degraded, text=sig.hint)
        if kind == "overseer_digest":
            return {"text": overseer.build_digest(**args)}
        if kind == "reach_parse":
            return {"verdict": reach_judge.parse_judge_text(args["raw"])}
        if kind == "reach_block":
            return {"text": reach_judge.verdict_block(args["verdict"], web_configured=bool(args.get("web")))}
        if kind == "quest_rank":
            def call(prompt, tool):  # noqa: ARG001
                if args["raw"] == "__raise__":
                    raise RuntimeError("judge down")
                return args["raw"]
            res = quest_judge.rank_quests(args["items"], args["quests"], call, home_quest_id=args.get("home"),
                                          fallback_order=args.get("fallback"))
            return {"results": [[c, r] for c, r in res]}
        if kind == "sufficiency":
            items = sufficiency.collect_abridged_items(args["cards"])
            pending = sufficiency.unfetched_abridged(items, args["executed"])
            return {"pending": [i.label for i in pending],
                    "notice": bool(sufficiency.render_abridged_notice(pending))}
        raise ValueError(kind)
    return judge
