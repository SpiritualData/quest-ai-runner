"""The closing's usage: tokens and calls of the whole turn and the flat-plan basis, read from what the orchestrator and provider already count."""
import dataclasses
from types import SimpleNamespace

from quest_ai_runner.the_consciousness.terminal.turn_usage import on_subscription, turn_usage


@dataclasses.dataclass
class NewFinished:
    tokens: int = 0
    calls: int = 0
    cost_basis: str = ""


@dataclasses.dataclass
class OldFinished:
    tokens: int = 0


class ClaudeCliProvider:
    usage_limits = object()
    fresh_input_tokens = 0


def test_a_plain_answer_turn_reports_its_tokens_calls_and_the_flat_plan_basis():
    provider = SimpleNamespace(primary=ClaudeCliProvider(), call_count=3)
    final = SimpleNamespace(tokens_in=1000, tokens_out=200)
    assert turn_usage(final, provider, NewFinished) == {"tokens": 1200, "calls": 3, "cost_basis": "subscription"}


def test_a_billed_lane_has_no_subscription_basis_and_nothing_unknown_is_invented():
    provider = SimpleNamespace(primary=SimpleNamespace(), call_count=0)
    assert turn_usage(None, provider, NewFinished) == {}
    assert not on_subscription(provider)
    assert turn_usage(SimpleNamespace(tokens_in=5, tokens_out=0), provider, NewFinished) == {"tokens": 5}


def test_an_older_hub_without_the_fields_is_left_alone():
    provider = SimpleNamespace(primary=ClaudeCliProvider(), call_count=2)
    assert turn_usage(SimpleNamespace(tokens_in=1, tokens_out=1), provider, OldFinished) == {"tokens": 2}
