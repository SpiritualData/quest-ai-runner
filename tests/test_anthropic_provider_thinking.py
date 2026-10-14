"""AnthropicProvider turns extended thinking off by default (offline, fake SDK client).

A reasoning model spends max_tokens on a thinking block before any text, so short planner and
answer calls can return no text and run slow. The provider sends ``thinking={"type": "disabled"}``
unless ``QAR_ANTHROPIC_THINKING=on``, and omits it when the SDK's ``create`` has no such parameter.
"""
import pytest

from quest_ai_runner.adapters.anthropic_provider import AnthropicProvider, _thinking_kwargs

TOOL = {"name": "decide", "description": "d", "input_schema": {"type": "object", "properties": {}}}


class _Block:
    def __init__(self, type_, **kw):
        self.type = type_
        self.__dict__.update(kw)


class _Resp:
    def __init__(self, content):
        self.content = content


class _ThinkingMessages:
    """create() declares ``thinking`` like the current SDK does."""
    def __init__(self):
        self.calls = []

    def create(self, *, model, max_tokens, messages, system=None, tools=None, tool_choice=None,
               thinking=None):
        self.calls.append({"thinking": thinking, "tool_choice": tool_choice, "tools": tools})
        if tool_choice:
            return _Resp([_Block("tool_use", name="decide", input={"mode": "answer"})])
        return _Resp([_Block("text", text="ok")])


class _OldMessages:
    """create() has no ``thinking`` parameter, like an older SDK."""
    def __init__(self):
        self.calls = []

    def create(self, *, model, max_tokens, messages, system=None, tools=None, tool_choice=None):
        self.calls.append({"tool_choice": tool_choice})
        return _Resp([_Block("text", text="ok")])


class _Client:
    def __init__(self, messages):
        self.messages = messages


def _provider(messages):
    p = AnthropicProvider(api_key="offline")
    p._client = _Client(messages)
    p.resolve_model = lambda m: m  # no live model listing
    return p


@pytest.fixture(autouse=True)
def _default_env(monkeypatch):
    monkeypatch.delenv("QAR_ANTHROPIC_THINKING", raising=False)


def test_plan_and_answer_disable_thinking_by_default():
    m = _ThinkingMessages()
    p = _provider(m)
    assert p.plan("pick", model="haiku", tool_schema=TOOL) == {"mode": "answer"}
    p.answer([{"role": "user", "content": "hi"}], model="haiku")
    p.answer([], model="haiku", layers=[{"cache": False, "text": "tail"}])
    assert [c["thinking"] for c in m.calls] == [{"type": "disabled"}] * 3
    # plan() still forces the tool: disabled thinking is the setting compatible with that.
    assert m.calls[0]["tool_choice"] == {"type": "tool", "name": "decide"}


def test_env_opt_in_leaves_thinking_to_the_model(monkeypatch):
    monkeypatch.setenv("QAR_ANTHROPIC_THINKING", "on")
    m = _ThinkingMessages()
    _provider(m).answer([{"role": "user", "content": "hi"}], model="haiku")
    assert m.calls[0]["thinking"] is None


def test_sdk_without_thinking_param_is_not_sent_the_kwarg():
    m = _OldMessages()
    p = _provider(m)
    assert p.answer([{"role": "user", "content": "hi"}], model="haiku") == "ok"
    assert _thinking_kwargs(m.create) == {}


def test_web_search_disables_thinking_too():
    class _WS(_ThinkingMessages):
        def create(self, *, model, max_tokens, messages, tools=None, thinking=None):
            self.calls.append({"thinking": thinking})
            return _Resp([_Block("text", text="a", citations=None)])
    m = _WS()
    _provider(m).web_search("q", model="haiku")
    assert m.calls[0]["thinking"] == {"type": "disabled"}
