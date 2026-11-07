"""Rebuild cases.json for the route-a-request metric from invented requests whose route is known. They name Quest things (goals, milestones, a quest's chat) and cover the failures this
metric exists to end: a status question that must not start a deep run, a count question that must be answered from a read, a bare cue naming a milestone, which asks to move it, and a
draft that must be written in the reply.

    python3 evaluation/components/route_a_request/build_cases.py

A case is a request and the view of the quest the Mind already has; ``expect`` is the route. Standard library only."""
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
VIEW = "Milestones on this quest: newsletter subscribers; podcast listeners; workshop signups. Open goals: 14."
INFORMATION = [
    "Where do we stand on the newsletter subscribers milestone?", "How many goals are open on this quest?", "What is the status of the workshop signups?",
    "Which of my goals is due first?", "Who owns the podcast milestone?", "What did we decide about the launch date in last week's notes?",
    "Summarise where this quest stands in a few sentences.", "What is the done-when for the listeners milestone?", "How many goals did we finish last month?",
    "Did anyone reply to the survey decision?", "What moved on the signups metric this week?", "Is there already a weekly check-in workflow on this quest?",
]
ACT = [
    "Add a goal for this week: send the spring survey to the list.", "Mark the workshop outline goal as done.", "Move the launch call to Thursday afternoon.",
    "Create a note on the podcast milestone saying the guest confirmed.", "Assign the invoice follow-up goal to Dana.", "Set up a weekly Monday check-in workflow for this quest.",
    "Start a research run on competitor pricing and report back when it is done.", "Schedule a reminder for Friday to renew the domain.",
    "Push the workshop signups milestone forward this week.", "Send the agenda to the team before the call.",
]
CUES = ["newsletter subscribers", "podcast listeners", "workshop signups"]
DRAFT = [
    "Write a short caption for the launch photo.", "Draft two versions of the subject line for Friday's email.", "Give me a three sentence description of the workshop for the website.",
    "Compose a thank you note to the guest, I will send it myself.", "Write the opening paragraph of the newsletter.", "Can you word a post announcing the new episode?",
    "Draft the message I could send to the volunteers about Saturday.", "Write two Facebook posts for the signups push, with a hypothesis for each.",
]
CLARIFY = [
    "Do the thing from before.", "Update it.", "Send that to them.", "Make it better.", "and also the other one", "Move it to next week.", "Fix the numbers.",
    "Can you look at the one Dana mentioned?",
]


def case(prefix, index, request, route, view=""):
    return {"id": f"qroute-{prefix}-{index:02d}", "kind": route, "args": {"request": request, "context": {"view": view} if view else {}}, "expect": route}


def main():
    cases = [case("information", i, t, "information", VIEW) for i, t in enumerate(INFORMATION, 1)]
    cases += [case("act", i, t, "act", VIEW) for i, t in enumerate(ACT, 1)]
    cases += [case("cue", i, t, "act", VIEW) for i, t in enumerate(CUES, 1)]
    cases += [case("draft", i, t, "draft", VIEW) for i, t in enumerate(DRAFT, 1)]
    cases += [case("clarify", i, t, "clarify") for i, t in enumerate(CLARIFY, 1)]
    (HERE / "cases.json").write_text(json.dumps(cases, indent=1) + "\n")
    print(len(cases), "cases")


if __name__ == "__main__":
    main()
