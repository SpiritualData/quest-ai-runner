"""FLOOR (``narrate-a-turn:no_beat``): no model call. The first beat is a fixed line chosen from the turn's mode and first event (a
new message that is a question or one that is not), relayed through the same Narrator. The grader still reads the line against the
same cases, so the floor is scored exactly as the call is."""
import claude_judge
from narrate_a_turn import arms, score

QUESTION_LINE = "Let me check on that."
STATEMENT_LINE = "On it."


def build(meter):
    provider = claude_judge.cli_provider(meter, "grader-only")   # the only model calls are the grader's

    def narrate(case):
        line = QUESTION_LINE if case["message"].rstrip().endswith("?") else STATEMENT_LINE
        got = arms.fixed_beat(case, line)
        return score.judged(case, got, provider)
    return narrate
