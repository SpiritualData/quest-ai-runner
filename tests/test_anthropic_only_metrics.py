"""Metrics run on Anthropic models (an organisation decision): a measured run refuses another vendor unless it is allowed on purpose."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evaluation" / "components"))
import common  # noqa: E402


def test_the_default_and_claude_tier_overrides_pass():
    common.require_anthropic({})
    common.require_anthropic({"QAR_MODEL_BACKEND": "claude_cli", "QAR_MODEL_FAST": "haiku", "QAR_MODEL_BEST": "claude-sonnet-5-5"})


def test_another_backend_or_tier_model_is_refused_unless_allowed_on_purpose():
    with pytest.raises(SystemExit):
        common.require_anthropic({"QAR_MODEL_BACKEND": "gemini"})
    with pytest.raises(SystemExit):
        common.require_anthropic({"QAR_MODEL_FAST": "gemini-3.1-flash-lite"})
    common.require_anthropic({"QAR_MODEL_BACKEND": "gemini", "QAR_EVAL_ALLOW_NON_ANTHROPIC": "1"})
