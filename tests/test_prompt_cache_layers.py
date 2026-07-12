"""Cache-economics layering (HANDS_FREE_QUEST_AI_DESIGN.md section 6, WS4 steps 3-4).

Proves the shared prompt-assembly path and provider cache wiring:

(a) plan / re-plan / answer / verify prompts built through the shared helper share a byte-identical
    L1 + L2 prefix and differ ONLY in the volatile tail.
(b) within-card item order is stable (by item id), independent of shuffled relevance scores.
(c) AnthropicProvider renders cache_control breakpoints on the cached blocks and never exceeds the
    four-breakpoint ceiling.
(d) the Gemini adapter passes the L1 head as native system_instruction when layers are given.
(e) plain-string calls (no layers) build byte-identical provider requests to before.

All offline: providers use fake SDK clients, so no network and no API key.
"""
from quest_ai_runner.adapters.anthropic_provider import AnthropicProvider, build_cached_system
from quest_ai_runner.adapters.card_content_render import render_card_content_blocks
from quest_ai_runner.adapters.gemini_provider import GeminiProvider, split_layers_for_gemini
from quest_ai_runner.core.prompt_layers import (
    PromptLayers,
    cache_control_indices,
    compose_layers,
    turn_prompt_head,
)


# --------------------------------------------------------------------------- #
# (a) One shared assembly path -> byte-identical L1 + L2, only the tail differs
# --------------------------------------------------------------------------- #

def test_shared_layers_have_byte_identical_prefix_across_call_kinds():
    persona = "You are the assistant persona for this whole conversation."
    standards = "Be rigorous. Never use em dashes."
    context = "CARD alpha\n  - (note) a thing\nCARD beta\n  - (note) another thing"

    plan = compose_layers(persona=persona, standards=standards, context=context,
                          tail="PLANNER INSTRUCTIONS\nthe user message")
    replan = compose_layers(persona=persona, standards=standards, context=context,
                            tail="PLANNER INSTRUCTIONS\nthe user message\nGATHERED: obs-1")
    answer = compose_layers(persona=persona, standards=standards, context=context,
                            tail="ANSWER INSTRUCTIONS\nwrite the reply now")
    verify = compose_layers(persona=persona, standards=standards, context=context,
                            tail="VERIFY INSTRUCTIONS\nworker output to judge")
    calls = [plan, replan, answer, verify]

    # L1 head and L2 context are each byte-identical across every call kind.
    assert len({c.head for c in calls}) == 1
    assert len({c.context for c in calls}) == 1
    # The cache-eligible prefix (L1 + L2) is therefore one shared byte string.
    assert len({c.prefix() for c in calls}) == 1
    # Every rendered prompt begins with that exact shared prefix ...
    shared_prefix = plan.prefix()
    for c in calls:
        assert c.render().startswith(shared_prefix)
    # ... and the four differ (only their tails vary).
    assert len({c.render() for c in calls}) == 4


def test_blocks_mark_head_and_context_cacheable_tail_volatile():
    layers = PromptLayers(head="HEAD", context="CTX", tail="TAIL")
    blocks = layers.blocks()
    assert blocks == [
        {"text": "HEAD", "cache": True},
        {"text": "CTX", "cache": True},
        {"text": "TAIL", "cache": False},
    ]
    # The prefix is always a true byte-prefix of the full render.
    assert layers.render().startswith(layers.prefix())
    # An empty layer is dropped from blocks (no empty cached block), tail always present.
    empty_head = PromptLayers(head="", context="CTX", tail="TAIL").blocks()
    assert empty_head == [{"text": "CTX", "cache": True}, {"text": "TAIL", "cache": False}]


def test_turn_prompt_head_is_deterministic_in_its_inputs():
    a = turn_prompt_head("p", "s")
    b = turn_prompt_head("p", "s")
    assert a == b and a != ""
    assert turn_prompt_head("", "") == ""


# --------------------------------------------------------------------------- #
# (b) Within-card item order is stable by id, not by relevance
# --------------------------------------------------------------------------- #

def _note(item_id: str, why: str, text: str, ts: float):
    return {"id": item_id, "type": "note", "why": why,
            "locator": {"text": text}, "ts": ts}


def test_within_card_item_order_stable_across_shuffled_relevance():
    items = [
        _note("item-b", "about foxes", "alpha beta foxes", ts=3.0),
        _note("item-a", "about dogs", "gamma delta dogs", ts=1.0),
        _note("item-c", "about cats", "epsilon cats", ts=2.0),
    ]
    # Same items, different INPUT order and different task keywords (so the relevance ranking that
    # SELECTS them differs). The RENDERED block order must be identical: stable by item id.
    blocks_foxes = render_card_content_blocks(
        {"id": "card-1", "content": list(items)}, {}, task_kws={"foxes"})
    blocks_dogs = render_card_content_blocks(
        {"id": "card-1", "content": list(reversed(items))}, {}, task_kws={"dogs", "cats"})

    ids_foxes = [b["id"] for b in blocks_foxes]
    ids_dogs = [b["id"] for b in blocks_dogs]
    assert ids_foxes == ids_dogs == sorted(ids_foxes) == ["item-a", "item-b", "item-c"]
    # Relevance survives as metadata (priority_rank), so usefulness info is not lost.
    assert all("priority_rank" in b for b in blocks_foxes)


# --------------------------------------------------------------------------- #
# Anthropic + Gemini fake SDK clients (offline)
# --------------------------------------------------------------------------- #

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


class _FakeAnthropicClient:
    def __init__(self):
        self.messages = _FakeMessages()


def _anthropic_with_fake():
    p = AnthropicProvider(api_key="offline")
    p._client = _FakeAnthropicClient()
    return p, p._client


class _FakeGeminiResp:
    def __init__(self, text):
        self.text = text


class _FakeGeminiModels:
    def __init__(self):
        self.last = None

    def generate_content(self, **kwargs):
        self.last = kwargs
        return _FakeGeminiResp("ok")


class _FakeGeminiClient:
    def __init__(self):
        self.models = _FakeGeminiModels()


def _gemini_with_fake():
    p = GeminiProvider(api_key="offline")
    p._client = _FakeGeminiClient()
    return p, p._client


# --------------------------------------------------------------------------- #
# (c) Anthropic cache_control breakpoints, capped at four
# --------------------------------------------------------------------------- #

def test_anthropic_answer_layers_emit_cache_control_on_cached_blocks():
    p, fake = _anthropic_with_fake()
    layers = PromptLayers(head="HEAD", context="CTX", tail="TAIL").blocks()
    out = p.answer([{"role": "user", "content": "fallback"}],
                   model="claude-sonnet-4-6", system="voice contract", layers=layers)
    assert out == "ok"
    system = fake.messages.last_kwargs["system"]
    # system is an array: the plain voice string first (uncached), then head + context, each cached.
    assert isinstance(system, list)
    assert system[0] == {"type": "text", "text": "voice contract"}
    cached = [b for b in system if "cache_control" in b]
    assert [b["text"] for b in cached] == ["HEAD", "CTX"]
    assert all(b["cache_control"] == {"type": "ephemeral"} for b in cached)
    # The volatile tail is the single user turn (the fallback message list is not used).
    assert fake.messages.last_kwargs["messages"] == [{"role": "user", "content": "TAIL"}]


def test_anthropic_plan_layers_emit_cache_control_and_keep_tools():
    p, fake = _anthropic_with_fake()
    layers = PromptLayers(head="HEAD", context="CTX", tail="TAIL").blocks()
    tool = {"name": "decide", "input_schema": {"type": "object"}}

    # No structured tool_use block in the fake response -> plan raises after reading usage; we only
    # care about the REQUEST it built, so capture that and ignore the (expected) raise.
    try:
        p.plan("fallback prompt", model="claude-sonnet-4-6", tool_schema=tool, layers=layers)
    except RuntimeError:
        pass
    kwargs = fake.messages.last_kwargs
    assert kwargs["tools"] == [tool]
    assert kwargs["tool_choice"] == {"type": "tool", "name": "decide"}
    cached = [b for b in kwargs["system"] if "cache_control" in b]
    assert [b["text"] for b in cached] == ["HEAD", "CTX"]
    assert kwargs["messages"] == [{"role": "user", "content": "TAIL"}]


def test_build_cached_system_never_exceeds_four_breakpoints():
    # Six cache=True blocks + a tail: only four may carry a cache_control marker.
    blocks = [{"text": f"b{i}", "cache": True} for i in range(6)]
    blocks.append({"text": "tail", "cache": False})
    system_array, tail_text = build_cached_system(None, blocks)
    marked = [b for b in system_array if "cache_control" in b]
    assert len(marked) == 4
    # The LAST four cached blocks keep the markers (they cache the longest prefixes).
    assert [b["text"] for b in marked] == ["b2", "b3", "b4", "b5"]
    assert tail_text == "tail"


def test_cache_control_indices_caps_and_keeps_last():
    blocks = [{"cache": True}, {"cache": True}, {"cache": True}, {"cache": True},
              {"cache": True}, {"cache": False}]
    assert cache_control_indices(blocks, max_breakpoints=4) == [1, 2, 3, 4]
    assert cache_control_indices(blocks, max_breakpoints=10) == [0, 1, 2, 3, 4]


# --------------------------------------------------------------------------- #
# (d) Gemini passes the head as native system_instruction
# --------------------------------------------------------------------------- #

def test_gemini_split_uses_head_as_system_instruction():
    layers = PromptLayers(head="HEAD", context="CTX", tail="TAIL").blocks()
    system_instruction, contents = split_layers_for_gemini("voice", layers)
    assert system_instruction == "voice\n\nHEAD"
    # Context stays first in contents (stable prefix), tail after it; the head is NOT duplicated.
    assert contents == "CTX\n\nTAIL"
    assert "HEAD" not in contents


def test_gemini_answer_layers_pass_system_instruction():
    p, fake = _gemini_with_fake()
    layers = PromptLayers(head="HEAD", context="CTX", tail="TAIL").blocks()
    out = p.answer([{"role": "user", "content": "ignored fallback"}],
                   model="gemini-2.0-flash", system="voice", layers=layers)
    assert out == "ok"
    kwargs = fake.models.last
    assert kwargs["config"]["system_instruction"] == "voice\n\nHEAD"
    assert kwargs["contents"] == "CTX\n\nTAIL"


def test_gemini_plan_layers_pass_system_instruction_and_json_mode():
    p, fake = _gemini_with_fake()
    layers = PromptLayers(head="HEAD", context="CTX", tail="TAIL").blocks()
    tool = {"name": "decide", "input_schema": {"type": "object"}}
    # Fake returns "ok" which won't parse as JSON; plan falls back to {} but still builds the request.
    p.plan("fallback prompt", model="gemini-2.0-flash", tool_schema=tool, layers=layers)
    kwargs = fake.models.last
    assert kwargs["config"]["system_instruction"] == "HEAD"
    assert kwargs["config"]["response_mime_type"] == "application/json"
    assert kwargs["contents"] == "CTX\n\nTAIL"


# --------------------------------------------------------------------------- #
# (e) Plain-string calls (no layers) are byte-identical to before
# --------------------------------------------------------------------------- #

def test_anthropic_plain_string_calls_unchanged_without_layers():
    p, fake = _anthropic_with_fake()
    # answer: message list + system string pass straight through, no system array.
    p.answer([{"role": "user", "content": "hello"}], model="claude-sonnet-4-6", system="be brief")
    assert fake.messages.last_kwargs["messages"] == [{"role": "user", "content": "hello"}]
    assert fake.messages.last_kwargs["system"] == "be brief"

    # plan: the plain prompt is the single user turn, no system key at all.
    tool = {"name": "decide", "input_schema": {"type": "object"}}
    try:
        p.plan("PLAIN PROMPT", model="claude-sonnet-4-6", tool_schema=tool)
    except RuntimeError:
        pass
    assert fake.messages.last_kwargs["messages"] == [{"role": "user", "content": "PLAIN PROMPT"}]
    assert "system" not in fake.messages.last_kwargs


def test_gemini_plain_string_answer_unchanged_without_layers():
    p, fake = _gemini_with_fake()
    p.answer([{"role": "user", "content": "hello"}], model="gemini-2.0-flash", system="be brief")
    kwargs = fake.models.last
    # No layers -> historic flatten path: one contents string, no system_instruction config.
    assert "System: be brief" in kwargs["contents"]
    assert "hello" in kwargs["contents"]
    assert "config" not in kwargs or not kwargs.get("config")
