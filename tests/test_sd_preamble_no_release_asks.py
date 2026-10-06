"""The SD shared-runner preamble tells every task that production release is not its concern."""
from pathlib import Path

PREAMBLE = Path(__file__).resolve().parents[3] / "setup" / "sd-shared-runner" / "context_preamble.md"


def _text() -> str:
    return PREAMBLE.read_text(encoding="utf-8")


def test_release_is_not_part_of_done():
    t = _text()
    assert "not fully done" in t
    assert "committed on the current month branch" in t
    assert "never create a decision, ask, task or note asking" in t


def test_production_only_when_original_message_asked():
    t = _text()
    assert "original human message explicitly asked for a production release" in t
    assert "is NOT such a request" in t


def test_zee_can_self_release():
    t = _text()
    assert "Zee can release himself" in t and "not the sduser account" in t
