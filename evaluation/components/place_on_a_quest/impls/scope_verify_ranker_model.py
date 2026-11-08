"""ARM ``place-on-a-quest:scope-verify``: the example-vote arm's second stage (candidates in the fused order, the backend's prompt with the
earlier items, a model picks), then the backend's verify step (``quest_verify``) on that pick.

The verify step shows a model ONLY the message, the picked quest in full and up to ``RUNNERS_UP`` other quests in fused order, and asks whether the
picked quest's own scope excludes the message (the model quotes the excluding words, which must be in the scope) and which listed quest holds it.
A switch is accepted only to a listed quest. In ``disagree`` mode (the backend default) the step runs only when the pick is not the example
vote's leader (or there is no vote); ``MODE`` here follows the backend's ``QUEST_VERIFY``. Counted model calls: one for the pick, one more when
the step runs; always pass ``--max-calls``."""
import json
import re

import live_arm
from context_selection.embedder import embedder
from place_on_a_quest import account
from place_on_a_quest.backend_loader import backend, examples as backend_examples, verify
from place_on_a_quest.impls import example_vote_ranker
from place_on_a_quest.model_pick import JUDGE_CAP, pick_among, shown

ALTERNATIVE = True   # measures the quest-backend code, which esoi cannot see from this repository
PAID = True
MODE = "disagree"


def answer_of(provider, model, system, prompt):
    raw = provider.answer([{"role": "user", "content": system + "\n\n" + prompt}], model=model)
    found = re.search(r"\{.*\}", str(raw), re.S)
    try:
        return json.loads(found.group(0)) if found else {}
    except ValueError:
        return {}


def build(meter):
    pick, _ = backend()
    V = verify()
    provider, model = live_arm.build(meter, "subscription")
    rank = example_vote_ranker.hybrid_candidate_ranker.build(meter)
    embed = embedder()

    def place(case):
        first = rank(case)["ranked"]
        examples = example_vote_ranker.similar_examples(case, embed)
        order, vote = example_vote_ranker.fused_order(case, first, examples)
        by_id = {q["quest_id"]: q for q in account.quests_of(case)}
        ordered = [by_id[i] for i in order]
        picked = pick_among(provider, model, pick, ordered, {**case, "examples": examples}, JUDGE_CAP)
        calls = 1
        voted = backend_examples().vote_order(vote)
        if picked and V.should_verify(MODE, picked[0], voted[0] if voted else None):
            candidates = [shown(q) for q in ordered]
            chosen = next(c for c in candidates if c["quest_id"] == picked[0])
            others = [c for c in candidates if c["quest_id"] != picked[0]][:V.RUNNERS_UP]
            verdict = V.parse_verdict(answer_of(provider, model, V.QUEST_VERIFY_SYSTEM_PROMPT, V.verify_prompt(case["message"], chosen, others)),
                                      chosen, others)
            calls += 1
            if verdict["switch"]:
                picked = [verdict["switch"]] + picked[1:]
        return {"picked": picked, "ranked": [q["quest_id"] for q in ordered], "calls": calls}
    return place
