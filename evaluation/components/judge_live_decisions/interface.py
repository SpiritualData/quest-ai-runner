"""The live-judge function (the model-decided half of ``judge-a-decision`` and the reach judge of ``act-in-the-world``).
Every solution is ONE function:

    judge(kind: str, args: dict) -> dict

``quest_rank`` (args ``items``, ``quests``, ``home``) returns ``{"chosen": [quest id or None per message]}``: the quest each message is
clearly about. ``reach`` (args ``message``, ``reach_summary``) returns ``{"reach": "inside" | "outside" | "world", "covered": bool}``:
where what the request needs lives, and whether an attached environment covers it. The cases are hand-labelled on synthetic quests and
include the ones the fallback gets right (a message about the home quest, a message about nothing), so a call has to earn its price on
the ones the fallback gets wrong.

Two arms on the same 12 cases: ``no_call_floor`` (offline: the call is dropped, so the quest ranker's own fallback and no reach verdict,
which leaves the planner planning as for a request inside the readable sources) and ``live_judges`` (the shipped prompts on a small model
through the Claude Code subscription). ``judges`` measures the deterministic halves of the same judges with a scripted provider.
"""
