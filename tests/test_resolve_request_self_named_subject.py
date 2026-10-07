"""The request resolver must not ask a clarifying question about a message that names its subject.

Found live in Quest AI chat (2026-10-06): "Summarize my latest daily reflection." is five words, so
the cheap gate sent it to the resolver, which replied "CLARIFY: Which daily reflection entry should
be summarized?" and the turn ended on that question. "My latest" is resolved by reading the data,
not by asking. The rule lives in the resolver prompt, ahead of the CLARIFY rule.
"""
from quest_ai_runner.core.orchestrator import RESOLVE_REQUEST_PROMPT


def test_self_named_subject_rule_comes_before_the_clarify_rule():
    text = RESOLVE_REQUEST_PROMPT
    rule = text.find("names its own subject")
    clarify = text.find("reply: CLARIFY:")
    assert rule != -1 and clarify != -1 and rule < clarify
    assert "my latest" in text and "never CLARIFY on it" in text


def test_prompt_still_formats():
    out = RESOLVE_REQUEST_PROMPT.format(conv_context="", user_message="Summarize my latest daily reflection.")
    assert "Summarize my latest daily reflection." in out
