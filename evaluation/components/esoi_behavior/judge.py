"""The ESOI model judge: grades one reply against one case's ``expected_behavior`` and ``failure_mode``.

The rubric is the case's own expected behaviour and failure mode (derived from the evolvable-structure
skill's principle for that case). The judge never sees the regexes or the reference replies, and is told to
grade what the reply DOES, not which words it uses, so standing instructions cannot win on vocabulary alone.
It runs through the keyless claude CLI at the cheapest tier (``QAR_EVAL_JUDGE_MODEL``, else
``QAR_EVAL_MODEL``, else haiku) with every tool disabled. Its output is one JSON object:
``{"verdict": "pass" | "fail", "reason": "<one line>"}``.

A caveat that belongs to the metric: when the judge and the replies are the same small model, the judge may
favour its own phrasing. ``esoi_judge`` measures its agreement with labelled replies; it does not remove that
risk.
"""
import os

import common  # noqa: F401  (sets sys.path for the repo)

SYSTEM = """You grade one reply of an AI assistant for one test case. Grade what the reply DOES, not which words it uses:
a reply that does the expected thing in different words passes; a reply that uses the right vocabulary but does
the failure mode fails. Extra length or polish earns nothing. Adding structure (a framework, a registry, a
process) is correct only when the expected behavior asks for it; when the expected behavior says the small fix
is right, a reply that builds structure fails.

Reply with only one JSON object: {"verdict": "pass" or "fail", "reason": "<one short line, no em dashes>"}."""

MAX_REPLY_CHARS = 6000


def judge_prompt(case, reply):
    """The user message for one grading call."""
    return (f"SCENARIO sent to the assistant:\n{case['scenario']}\n\n"
            f"EXPECTED BEHAVIOR:\n{case['expected_behavior']}\n\n"
            f"FAILURE MODE (a reply that does this fails):\n{case['failure_mode']}\n\n"
            f"REPLY TO GRADE:\n{reply[:MAX_REPLY_CHARS]}\n\nGrade the reply.")


def parse_verdict(text):
    """``(passed, reason)`` from the judge's text. An unparseable answer is a fail, never a pass."""
    from quest_ai_runner.adapters.claude_cli_provider import extract_json_object
    obj = extract_json_object(text or "")
    verdict = str(obj.get("verdict", "")).strip().lower()
    reason = " ".join(str(obj.get("reason", "")).split())
    if verdict not in ("pass", "fail"):
        return False, "unparseable judge output: " + " ".join((text or "").split())[:80]
    return verdict == "pass", reason


def judge_model():
    return os.environ.get("QAR_EVAL_JUDGE_MODEL") or os.environ.get("QAR_EVAL_MODEL", "haiku")


def build_judge(meter, provider_class=None):
    """``judge(case, reply) -> (passed, reason)``, every call counted by ``meter``. ``provider_class`` defaults to
    the claude CLI provider."""
    if provider_class is None:
        from quest_ai_runner.adapters.claude_cli_provider import ClaudeCliProvider as provider_class
    provider = meter.wrap(provider_class())
    model = judge_model()

    def judge(case, reply):
        text = provider.answer([{"role": "user", "content": judge_prompt(case, reply)}], model=model, system=SYSTEM)
        return parse_verdict(text)
    return judge
