"""A deterministic stand-in for the model, for the OFFLINE arm of the recipe fast path. It reads the user's
message with a few rules the way a competent model would and plays both roles the engine calls a model for:
the planner (``decide``: pick a tool and its arguments, or answer) and the replay check (``recipe_args``:
does this request repeat the saved operation, and with what arguments).

It is written against the case file with the cases in view, so it is an optimistic ceiling for what a model
decides. The arm therefore measures the REPLAY ENGINE (nomination, the one fill-args call, the mutating
guard, scope, learning from a success) given a model that judges well; it says nothing about how well a real
model judges. The live arm (the claude CLI) is ``recipe_replay`` and ``planner_only``."""
import re

QUESTION_START = re.compile(r"^(what|what's|how|do i|does|is|are|show|explain|give|which|where|why)\b", re.I)


def read_intent(message):
    """(tool | None, args) for one message."""
    text = message.strip()
    low = text.lower()
    if re.search(r"\bnot done yet\b|\bonly meant to ask\b", low):
        return None, {}
    habit = re.search(r"(?:my|the)\s+(\w+)\s+habit", low)
    name = habit.group(1) if habit else ""
    if re.search(r"streak|^show me my \w+ habit|^is my \w+ habit", low) and name:
        return "habit_streak", {"habit": name}
    if QUESTION_START.match(low):
        return None, {}
    log = re.search(r"log\s+(\d+)\s+minutes?\s+on\s+my\s+(\w+)\s+habit", low)
    if log:
        return "log_time", {"habit": log.group(2), "minutes": int(log.group(1))}
    goal = re.search(r"add a (\w+) goal called (.+)$", low)
    if goal:
        return "add_goal", {"title": goal.group(2).strip(), "period": goal.group(1)}
    note = re.search(r"add a note(?: saying|:)?\s+(.+)$", low)
    if note:
        return "add_note", {"text": note.group(1).strip()}
    if name and re.search(r"\b(mark|finished|did)\b", low):
        return "complete_habit", {"habit": name}
    return None, {}


class RuleModel:
    """A provider whose ``plan`` answers the planner and the replay check from ``read_intent``. ``current`` is
    set by the implementation to the message being handled. Every call a real model would have cost is
    counted on the meter."""

    def __init__(self, meter):
        self.meter, self.current, self.acted = meter, "", set()

    def plan(self, prompt, *, model, tool_schema, **kwargs):  # noqa: ARG002
        name = tool_schema.get("name")
        if name == "recipe_args":
            self.meter.record()
            tool, args = read_intent(self.current)
            wanted = re.search(r"TOOL: (\w+)", prompt)
            same = tool is not None and wanted is not None and wanted.group(1) == tool
            return {"applies": True, "args": args} if same else {"applies": False}
        if name == "decide":
            self.meter.record()
            tool, args = read_intent(self.current)
            if tool and self.current not in self.acted:
                self.acted.add(self.current)
                return {"action": "tool", "tool_calls": [{"name": tool, "args": args}], "rationale": "do it",
                        "model_tier": "haiku"}
            return {"action": "answer", "rationale": "done", "model_tier": "haiku"}
        return {"signal": "proceed", "reason": ""} if name == "oversee" else {"met": True}

    def answer(self, messages, *, model, system=None):  # noqa: ARG002
        return "OK"

    def list_models(self):
        return ["claude-opus-4-8", "claude-sonnet-4-6", "claude-haiku-4-5"]
