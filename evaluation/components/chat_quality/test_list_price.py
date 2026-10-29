"""The chat_quality cost estimate prices the served model at its own provider's input and output rate.
Run:  python3 -m pytest evaluation/components/chat_quality/test_list_price.py -q"""
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

from quest_ai_runner import pricing  # noqa: E402


def load_list_price():
    try:
        from chat_quality.impls.dev_quest_chat import list_price
    except ImportError as error:  # the live arm needs the dev tooling on the path
        pytest.skip(f"live arm not importable here: {error}")
    return list_price


def test_a_gemini_model_is_priced_at_its_own_input_and_output_rate():
    list_price = load_list_price()
    # 1M in at $0.25 plus 1M out at $1.50 (https://ai.google.dev/gemini-api/docs/pricing)
    assert list_price(["gemini-3.1-flash-lite"], 1_000_000, 1_000_000) == pytest.approx(1.75)


def test_an_anthropic_model_keeps_its_rate_and_the_five_times_output_ratio():
    list_price = load_list_price()
    assert list_price(["claude-sonnet-4-6"], 1_000_000, 1_000_000) == pytest.approx(3.0 + 15.0)


def test_pricing_knows_the_provider_of_a_model_by_name():
    assert pricing.provider_for_model("gemini-3.1-flash-lite") == "gemini"
    assert pricing.provider_for_model("gpt-4o") == "openai"
    assert pricing.provider_for_model("haiku") == "anthropic"
    assert pricing.get_output_cost_per_mtok("gemini", "gemini-3.1-flash-lite") == 1.50


def test_gemini_3_6_flash_is_priced_at_its_cited_rate():
    list_price = load_list_price()
    # $0.75 in, $3.75 out per 1M (https://ai.google.dev/gemini-api/docs/pricing, read 2026-10-29)
    assert list_price(["gemini-3.6-flash"], 1_000_000, 1_000_000) == pytest.approx(4.50)
