"""The two live routes of the model arms of hand_off_a_goal, draft_a_metric and label_context_cards.

``subscription``: the Claude Code subscription through ``claude -p`` on its cheapest tier. No API dollars; the dollar figure the
CLI reports is a list-price equivalent, and its token counts include the CLI's own harness, so they are not a prompt's size.
``fast_api``: the fast tier the shipped configuration pins (a Gemini flash-lite model), priced by ``prices`` in esoi.toml. It is the
route that tells what a prompt costs and how long it takes in production, so the per-prompt statistics of
``justified_model_spend`` come from it.

Both are pinned to one model whatever tier the caller asks for, and both are wrapped on the run's call meter. Stay on the ladder of
esoi/metric-run-policy.md (one case, three, then the set) and always pass ``--max-calls``."""
import os

SUBSCRIPTION_MODEL = "claude-haiku"
FAST_MODEL_ENV = "QAR_EVAL_LIVE_MODEL"
FAST_MODEL_DEFAULT = "gemini-3.1-flash-lite"


class Pinned:
    """A provider that answers with one model whatever model the caller names. ``plan``, ``answer`` and ``generate`` are the
    three entry points the measured code uses; ``generate`` is the host adapter the card updater's signature expects."""

    def __init__(self, provider, model):
        self.provider, self.model = provider, model

    def answer(self, messages, *, model=None, system=None):  # noqa: ARG002
        return self.provider.answer(messages, model=self.model, system=system)

    def plan(self, prompt, *, model=None, tool_schema, layers=None):  # noqa: ARG002
        return self.provider.plan(prompt, model=self.model, tool_schema=tool_schema, layers=layers)

    def generate(self, prompt, model=None, max_tokens=500, temperature=0.3):  # noqa: ARG002
        return self.provider.answer([{"role": "user", "content": prompt}], model=self.model)


def build(meter, route):
    """``(pinned provider, model name)`` for ``route`` (``subscription`` or ``fast_api``), counted on ``meter``."""
    if route == "subscription":
        from quest_ai_runner.adapters.claude_cli_provider import ClaudeCliProvider
        provider = meter.wrap(ClaudeCliProvider(claude_path="claude", timeout_seconds=420))
        meter.model = "claude-code-subscription"
        return Pinned(provider, SUBSCRIPTION_MODEL), SUBSCRIPTION_MODEL
    if route == "fast_api":
        from quest_ai_runner.adapters.gemini_provider import GeminiProvider
        model = os.environ.get(FAST_MODEL_ENV) or FAST_MODEL_DEFAULT
        provider = meter.wrap(GeminiProvider())
        meter.model = model
        return Pinned(provider, model), model
    raise ValueError(f"unknown route {route!r}")
