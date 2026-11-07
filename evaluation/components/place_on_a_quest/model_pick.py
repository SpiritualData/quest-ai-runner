"""The second stage both arms share: the backend's own numbered-candidates prompt (``quest_pick_prompt``) answered by a model on the Claude Code
subscription's cheapest tier, over a window of the first ``JUDGE_WINDOW`` candidates (the runner's constant, which the backend's ranker uses too, passed in by the arm). The arm decides only the ORDER of the candidates the model is shown; the pick is the model's alone and an empty
answer is none (``quest_selection``'s ``picked_sink``), so a message no quest fits is placed on none."""
import json
import re

from place_on_a_quest import account


def shown(quest):
    open_outcomes = "; ".join(quest["open"])
    return {"quest_id": quest["quest_id"], "title": account.quest_label(quest),
            "state": f"{quest['state']} | open measurable outcomes: {open_outcomes}", "scope": quest["scope"]}


def pick_among(provider, model, pick, ordered, case, window):
    """The ids the model picks for ``case`` among ``ordered`` quests (best candidate first), as the backend's prompt asks it."""
    ordered = ordered[:max(window, 1)]   # the model is shown a window of the first quests, as quest_selection.rank_quest_batch shows it
    items = [{"text": case["message"], "previous": case["previous"]}]
    prompt = pick.QUEST_RANKING_SYSTEM_PROMPT + "\n\n" + pick.pick_prompt([shown(q) for q in ordered], items, None, 3)
    raw = provider.answer([{"role": "user", "content": prompt}], model=model)
    found = re.search(r"\{.*\}", str(raw), re.S)
    try:
        answer = json.loads(found.group(0)) if found else {}
    except ValueError:
        answer = {}
    return pick.parse_picks(answer, 1, [q["quest_id"] for q in ordered]).get(0, [])
