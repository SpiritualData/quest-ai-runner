"""parse_json_with_retry: the standard helper that retries a JSON-producing call on parse failure."""
import json

import pytest

from quest_ai_runner.adapters.retry_utils import parse_json_with_retry


def test_retries_until_valid_json():
    seq = iter(["not json", "{bad", '{"action": "deep"}'])
    calls = {"n": 0}

    def produce():
        calls["n"] += 1
        return next(seq)

    got = parse_json_with_retry(produce, max_retries=3, base_delay=0.0, label="t")
    assert got == {"action": "deep"}
    assert calls["n"] == 3  # re-asked twice, succeeded on the third


def test_raises_after_exhausting_retries():
    with pytest.raises((ValueError, json.JSONDecodeError)):
        parse_json_with_retry(lambda: "nope", max_retries=2, base_delay=0.0, label="t")


def test_already_decoded_passes_through():
    assert parse_json_with_retry(lambda: {"a": 1}, base_delay=0.0) == {"a": 1}
    assert parse_json_with_retry(lambda: [1, 2], base_delay=0.0) == [1, 2]


def test_validate_rejects_then_retries():
    seq = iter(['{"x": 1}', '{"action": "answer"}'])

    def produce():
        return next(seq)

    # First parses but fails validation (no 'action'); second is accepted.
    got = parse_json_with_retry(
        produce, max_retries=2, base_delay=0.0,
        validate=lambda o: isinstance(o, dict) and "action" in o, label="t")
    assert got == {"action": "answer"}
