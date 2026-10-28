"""CURRENT, live (``narrate-a-turn:model_beat``): the library's Narrator makes its one cheap call for the first beat, exactly as a turn
does, on the Claude Code subscription (``claude -p``, haiku). PAID in the sense of model calls, not API dollars: the dollar figure the
run reports is a list-price equivalent. Stay on the ladder of esoi/metric-run-policy.md and always pass ``--max-calls``."""
PAID = True
import claude_judge
from narrate_a_turn import arms, score
from quest_ai_runner.core.orchestrator import Narrator


def build(meter):
    provider = claude_judge.cli_provider(meter, arms.MODEL)

    def narrate(case):
        got = arms.first_beat(case, provider, arms.MODEL, Narrator)
        return score.judged(case, got, provider)
    return narrate
