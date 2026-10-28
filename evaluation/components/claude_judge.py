"""One grader call on the Claude Code subscription (``claude -p``), shared by the call-value components
(narrate_a_turn, explain_an_answer, describe_an_attachment).

The grader call is tagged ``eval.judge`` so that ``justified_model_spend`` never counts it as a call of the prompt under test,
and it is metered like every other call so an arm's call, token and time totals include it. Keyless: the provider drops the
API key from the environment, so the run costs no API dollars; the dollar figures it reports are list-price equivalents."""
import common  # noqa: F401  (puts the repository on the path)
from esoi.harness import BudgetExceeded
from quest_ai_runner import usage
from quest_ai_runner.adapters.claude_cli_provider import ClaudeCliProvider, extract_json_object

JUDGE_MODEL = "claude-sonnet"   # family alias; the CLI resolves it to the newest sonnet
JUDGE_TAG = "eval.judge"
SYSTEM = ("You are a strict grader of one short piece of AI-written text. Judge only against the facts you are given. "
          "Reply with one JSON object and nothing else. Never use em dashes.")


def cli_provider(meter, model):
    """A metered ``claude -p`` provider; ``model`` is what the run records as its model."""
    provider = ClaudeCliProvider(claude_path="claude", timeout_seconds=180)
    meter.wrap(provider)
    meter.model = model
    return provider


def ask_json(provider, prompt, model=JUDGE_MODEL, tries=2):
    """The grader's JSON object, or ``{"error": ...}`` when it could not be got (a case with an error counts as not passed)."""
    last = "unparseable grader reply"
    for attempt in range(tries):
        usage.tag_spec(JUDGE_TAG)
        try:
            text = provider.answer([{"role": "user", "content": prompt}], model=model, system=SYSTEM)
        except BudgetExceeded:
            raise
        except Exception as exc:  # noqa: BLE001  (a grader outage is a failed case with its reason, not a crash)
            last = f"{type(exc).__name__}: {str(exc)[:140]}"
            continue
        finally:
            usage.tag_spec("")
        got = extract_json_object(text)
        if got:
            return got
    return {"error": last}
