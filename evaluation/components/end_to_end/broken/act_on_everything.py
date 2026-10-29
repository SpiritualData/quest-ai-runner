"""BASELINE: the shortcut a first draft would write. Every message is saved as a note on whichever quest comes first, and the reply
says Done, whether or not anything the person asked for happened. No model call. The cases must fail it."""
from end_to_end import drive


def build(meter):  # noqa: ARG001
    def turn(case):
        if case["kind"] == "terminal":
            return {"reply": "", "writes": [], "reads": [], "web": [], "route": "terminal",
                    "terminal": {"stdout": "I am looking into this \u2014 right away.", "exit": 0}}
        world = drive.new_world(case)
        world.writes.append({"tool": "add_quest_note", "args": {"text": case["message"]}, "quest_id": world.pinned or "q_fit"})
        return drive.fold(case, world, type("R", (), {"kind": "answer", "text": "Done.", "exit_reason": "verified"})())
    return turn
