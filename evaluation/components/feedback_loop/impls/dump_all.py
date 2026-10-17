"""BASELINE: remember every correction verbatim, whatever its kind, and show all of them on every later turn
(no ledger, no standing-versus-one-off distinction, no relevance). Perfect recall by construction, so it
shows what the real loop must beat on precision: a one-time request becomes a permanent rule and unrelated
rules crowd every prompt. Zero model calls."""


def build(meter):  # noqa: ARG001
    def replay(steps):
        heard, turns = [], []
        for step in steps:
            if step["op"] == "correct":
                heard.append(step["text"])
            else:
                turns.append("\n".join(heard))
        return {"turns": turns}
    return replay
