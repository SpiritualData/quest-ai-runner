"""LIVE, on the Claude Code subscription (``judge-a-decision:live_judges``): the shipped quest-ranking and reach-judge prompts with a
small model behind them (`claude -p`, cheapest tier), over the 12 hand-labelled cases. Its no-call floor is
``no_call_floor`` on the same cases. PAID in the sense of the run policy (subscription calls, no API dollars): pass
``--max-calls``. The eval model is not the deployment's configured tier (the reach judge ships
on the balanced tier), so the statistics say what the prompt costs on the cheapest tier."""
PAID = True
import json

import live_arm
from quest_ai_runner.core import quest_judge, reach_judge
from quest_ai_runner.core.card_filter import _extract_json


def build(meter):
    provider, model = live_arm.build(meter, "subscription")

    def call_judge(prompt, tool_schema):
        raw = provider.plan(prompt, model=model, tool_schema=tool_schema)
        if isinstance(raw, str):
            raw = json.loads(_extract_json(raw) or "{}")
        return raw if isinstance(raw, dict) else {}

    def judge(kind, args):
        if kind == "quest_rank":
            rows = quest_judge.rank_quests(args["items"], args["quests"], call_judge, home_quest_id=args.get("home"))
            return {"chosen": [chosen for chosen, ranking in rows]}
        if kind == "reach":
            raw = provider.plan(reach_judge.judge_prompt(args["message"], args["reach_summary"]), model=model,
                                tool_schema=reach_judge.REACH_JUDGE_TOOL)
            verdict = reach_judge.normalize_verdict(raw) or reach_judge.parse_judge_text(raw) or {"reach": "inside", "covered_by": None}
            return {"reach": verdict["reach"], "covered": bool(verdict["covered_by"])}
        raise ValueError(f"live_judges only answers the two kinds, not {kind}")
    return judge
