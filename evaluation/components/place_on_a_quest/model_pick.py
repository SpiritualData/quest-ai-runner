"""The second stage both arms share: the backend's own numbered-candidates prompt (``quest_pick_prompt``) answered by a model on the Claude Code
subscription's cheapest tier, over a window of the first ``JUDGE_CAP`` candidates (the backend's default cap on the quests the judge sees, ``QUEST_JUDGE_MAX_QUESTS``, 40; the arm passes it in) and, when the case carries ``examples`` (earlier items the person placed: ``{"text", "quest_id"}``), the backend's earlier-items block after the quests. The arm decides only the ORDER of the candidates the model is shown; the pick is the model's alone and an empty
answer is none (``quest_selection``'s ``picked_sink``), so a message no quest fits is placed on none."""
import json
import re

from place_on_a_quest import account, backend_loader


JUDGE_CAP = 40   # quest-backend quest_selection.DEFAULT_MAX_JUDGE_QUESTS: every active quest up to this many


def shown(quest):
    open_outcomes = "; ".join(quest["open"])
    return {"quest_id": quest["quest_id"], "title": account.quest_label(quest),
            "state": f"{quest['state']} | open measurable outcomes: {open_outcomes}", "scope": quest["scope"],
            **backend_loader.quest_aim(quest)}


def pick_among(provider, model, pick, ordered, case, window):
    """The ids the model picks for ``case`` among ``ordered`` quests (best candidate first), as the backend's prompt asks it."""
    ordered = ordered[:max(window, 1)]   # the model is shown a window of the first quests, as quest_selection.rank_quest_batch shows it
    items = [{"text": case["message"], "previous": case["previous"], "examples": case.get("examples") or []}]
    prompt = backend_loader.ranking_system_prompt() + "\n\n" + pick.pick_prompt([shown(q) for q in ordered], items, None, 3)
    raw = provider.answer([{"role": "user", "content": prompt}], model=model)
    found = re.search(r"\{.*\}", str(raw), re.S)
    try:
        answer = json.loads(found.group(0)) if found else {}
    except ValueError:
        answer = {}
    return pick.parse_picks(answer, 1, [q["quest_id"] for q in ordered]).get(0, [])
