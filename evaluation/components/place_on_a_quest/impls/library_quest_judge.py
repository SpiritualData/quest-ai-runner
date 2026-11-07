"""ARM ``place-on-a-quest:library-quest-judge`` (quest-ai-runner ``objectives/quest_judge.select_quest``): the library's own judge, which the
terminal uses when no host supplies the placement. No candidate generation: the model sees the account's quests in the order given and returns
the one it is about (or none), through a tool schema. Counted model calls on the Claude Code subscription; always pass ``--max-calls``."""
import json

import live_arm
from place_on_a_quest import account
from quest_ai_runner.the_intelligence.higher_mind.objectives.quest_judge import select_quest

PAID = True


def build(meter):
    provider, model = live_arm.build(meter, "subscription")

    def place(case):
        quests = [{"quest_id": q["quest_id"], "title": account.quest_label(q), "state": q["state"]} for q in account.quests_of(case)]
        def call_judge(prompt, tool_schema):
            raw = provider.plan(prompt, tool_schema=tool_schema)
            return json.loads(raw) if isinstance(raw, str) else raw
        chosen = select_quest(quests, case["message"], call_judge, previous_message=case["previous"])
        return {"picked": [chosen] if chosen else [], "ranked": [q["quest_id"] for q in quests], "calls": 1}
    return place
