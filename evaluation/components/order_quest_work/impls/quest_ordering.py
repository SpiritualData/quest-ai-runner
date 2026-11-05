"""CURRENT: Quest's arm of the hub's Prioritization, run as the autopilot runs it. Zero model calls."""
from quest_ai_runner.the_consciousness.prioritization.quest_ordering import QuestOrdering


def build(meter):  # noqa: ARG001
    arm = QuestOrdering()
    return lambda kind, args: [item["id"] for item in arm.prioritize(person=args["person"], candidates=args["candidates"], stated=args.get("stated"), now=args["now"])["ordered"]]
