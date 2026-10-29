"""FLOOR: no model call and no orchestrator. A question is answered "I do not have that in front of me", a request to add a goal
or a note is written as one, a request for a queued task is queued, and the terminal prints the library's fixed Queued line. What
a person asks beyond those shapes gets the same non-answer. It is the least a first draft does without a model."""
import re

from end_to_end import drive
from end_to_end.world import World

NONANSWER = "I do not have that in front of me, so I cannot say."


def build(meter):  # noqa: ARG001
    def turn(case):
        if case["kind"] == "terminal":
            return drive.run_terminal(case, None)
        world = drive.new_world(case)
        low = case["message"].lower().strip()
        qid = world.pinned
        if re.match(r"(queue|start|run)\b.*\btask\b", low) and qid:
            world.writes.append({"tool": "create_task", "args": {"text": case["message"]}, "quest_id": qid})
        elif re.match(r"add (a|two|three) goals?\b", low) and qid:
            world.writes.append({"tool": "create_quest_goal", "args": {"title": case["message"]}, "quest_id": qid})
        elif re.match(r"save this\b", low) and qid:
            world.writes.append({"tool": "add_quest_note", "args": {"text": case["message"]}, "quest_id": qid})
        reply = "Done." if world.writes else NONANSWER
        out = drive.fold(case, world, type("R", (), {"kind": "answer", "text": reply, "exit_reason": "verified"})())
        return out
    return turn
