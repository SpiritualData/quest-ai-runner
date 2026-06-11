"""Providers handle multimodal message content.

* AnthropicProvider.answer passes a LIST content (text + image blocks) THROUGH to the SDK
  unflattened, and the plain-string path is unchanged. Uses a fake SDK client so it's offline.
* ClaudeCliProvider._flatten_messages degrades a block list to text and never crashes.
"""
from quest_ai_runner.adapters.anthropic_provider import AnthropicProvider
from quest_ai_runner.adapters.claude_cli_provider import _flatten_block, _flatten_messages


class _FakeTextBlock:
    def __init__(self, text):
        self.type = "text"
        self.text = text


class _FakeResp:
    def __init__(self, text):
        self.content = [_FakeTextBlock(text)]


class _FakeMessages:
    def __init__(self):
        self.last_kwargs = None

    def create(self, **kwargs):
        self.last_kwargs = kwargs
        return _FakeResp("ok")


class _FakeClient:
    def __init__(self):
        self.messages = _FakeMessages()


def _provider_with_fake_client():
    p = AnthropicProvider(api_key="not-used-offline")
    fake = _FakeClient()
    p._client = fake  # inject so _get_client returns it without importing the SDK
    return p, fake


def test_anthropic_answer_passes_image_blocks_through_unflattened():
    p, fake = _provider_with_fake_client()
    image_block = {"type": "image",
                   "source": {"type": "base64", "media_type": "image/png", "data": "QUJD"}}
    messages = [{"role": "user", "content": [image_block, {"type": "text", "text": "what is this?"}]}]
    out = p.answer(messages, model="claude-sonnet-4-6")
    assert out == "ok"
    # The content LIST reached the SDK untouched (image block preserved, not stringified).
    sent = fake.messages.last_kwargs["messages"]
    assert sent[0]["content"][0]["type"] == "image"
    assert sent[0]["content"][0]["source"]["data"] == "QUJD"
    assert sent[0]["content"][1] == {"type": "text", "text": "what is this?"}


def test_anthropic_answer_plain_string_path_unchanged():
    p, fake = _provider_with_fake_client()
    messages = [{"role": "user", "content": "hello"}]
    out = p.answer(messages, model="claude-sonnet-4-6", system="be brief")
    assert out == "ok"
    assert fake.messages.last_kwargs["messages"] == [{"role": "user", "content": "hello"}]
    assert fake.messages.last_kwargs["system"] == "be brief"


def test_cli_flatten_block_handles_text_image_and_unknown():
    assert _flatten_block("plain") == "plain"
    assert _flatten_block({"type": "text", "text": "hi"}) == "hi"
    # An image block degrades to a placeholder note, never raises.
    note = _flatten_block({"type": "image",
                           "source": {"type": "base64", "media_type": "image/png", "data": "x"}})
    assert "image" in note.lower()
    # A block carrying a description uses it.
    assert _flatten_block({"type": "image", "description": "a red square"}) == "a red square"
    # An unknown shape doesn't crash.
    assert isinstance(_flatten_block({"weird": True}), str)


def test_cli_flatten_messages_with_block_list_does_not_crash():
    messages = [{
        "role": "user",
        "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "x"}},
            {"type": "text", "text": "describe it"},
        ],
    }]
    rendered = _flatten_messages(messages)
    assert "USER:" in rendered
    assert "describe it" in rendered
