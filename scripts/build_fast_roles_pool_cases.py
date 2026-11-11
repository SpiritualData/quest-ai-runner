"""Render QAR's own fast-role cases into the hub's pooled stage (``evaluation/fast_roles_pool`` of the hub of hubs).

The hub cannot import this package, so the cases whose prompts QAR owns are rendered here once, with QAR's real prompt text, into a plain JSON file the hub
stage reads: each case holds the finished prompt, how to read the answer and the label its source states. Sources (all already in this repository):

  * ``hand_off_a_goal`` doability and assignee cases: roles request_classification (doability) and representative_selection (assignee). The prompt is the one
    ``goal_handoff`` renders, captured by running the library's own code with a judge that records the prompt.
  * ``judge_live_decisions`` quest_rank cases and ``place_on_a_quest`` cases with one labelled quest or none (the three multi-quest cases are left out: the
    ranking prompt picks one quest per message): role request_classification, prompt ``judge-a-decision.quest-ranking``.

Run: ``PYTHONPATH=<esoi>:<hub> python3 scripts/build_fast_roles_pool_cases.py --out <hub>/evaluation/fast_roles_pool/datasets/consumer_cases.json``."""
import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
COMPONENTS = REPO / "evaluation" / "components"
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(COMPONENTS))

from hand_off_a_goal import arms  # noqa: E402
from place_on_a_quest import account  # noqa: E402
from quest_ai_runner.the_intelligence.higher_mind.objectives import goal_handoff, quest_judge  # noqa: E402
from quest_ai_runner.the_self.integrity.prompts.prompt_specs import render_prompt  # noqa: E402

RANK_FORMAT = ('\nAnswer with one JSON object only: {"results": [{"item": <message number>, "quest_id": "<quest id, or empty for none>"}]}')


def load(component):
    return json.loads((COMPONENTS / component / "cases.json").read_text(encoding="utf-8"))


def captured_prompt(case):
    """The prompt the library renders for a hand-off case, read by running its own code with a judge that only records it."""
    seen = []

    def judge(prompt):
        seen.append(prompt)
        return "{}"
    arms.run_case(case, judge)
    return seen[0]


def hand_off(cases):
    out = []
    for case in cases:
        label = case["label"]
        if case["kind"] == "doability":
            out.append(dict(id=f"pool-{case['id']}", kind="doability", role="request_classification", source=f"hand_off_a_goal/{case['id']}",
                            args=dict(prompt=captured_prompt(case)), expect=dict(ai_can_do=label["ai_can_do"])))
        else:
            ids = goal_handoff.human_member_ids(case["members"])
            out.append(dict(id=f"pool-{case['id']}", kind="assignee", role="representative_selection", source=f"hand_off_a_goal/{case['id']}",
                            args=dict(prompt=captured_prompt(case), member_ids=ids), expect=dict(assignee=label["assignee"], ai_can_do=label["ai_can_do"])))
    return out


def rank_prompt(items, quests, home):
    prompt = render_prompt("judge-a-decision.quest-ranking", quests=quest_judge._quest_lines(quests, home), messages=quest_judge._message_blocks(items))
    return prompt + RANK_FORMAT


def quest_rank(live, placed):
    out = []
    for case in live:
        if case["kind"] != "quest_rank":
            continue
        a = case["args"]
        out.append(dict(id=f"pool-{case['id']}", kind="quest_rank", role="request_classification", source=f"judge_live_decisions/{case['id']}",
                        args=dict(prompt=rank_prompt(a["items"], a["quests"], a["home"]), items=len(a["items"])),
                        expect=dict(chosen=case["expect"]["eq"]["chosen"])))
    shown = [{"quest_id": q["quest_id"], "title": account.quest_label(q),
              "state": f"{q['state']} | open measurable outcomes: {'; '.join(q['open'])}"} for q in account.CORE]
    for case in placed:
        if len(case["expect"]) > 1:
            continue
        items = [{"text": case["message"], "previous": case["previous"]}]
        out.append(dict(id=f"pool-{case['id']}", kind="quest_rank", role="request_classification", source=f"place_on_a_quest/{case['id']}",
                        args=dict(prompt=rank_prompt(items, shown, None), items=1), expect=dict(chosen=[case["expect"][0] if case["expect"] else None])))
    return out


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True)
    cases = hand_off(load("hand_off_a_goal")) + quest_rank(load("judge_live_decisions"), load("place_on_a_quest"))
    out = Path(parser.parse_args().out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(cases, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(len(cases), "cases")


if __name__ == "__main__":
    main()
