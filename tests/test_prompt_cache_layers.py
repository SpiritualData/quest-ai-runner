"""Cache-economics layering (HANDS_FREE_QUEST_AI_DESIGN.md section 6, WS4 steps 3-4).

Proves the shared prompt-assembly path and provider cache wiring:

(a) plan / re-plan / answer / verify prompts built through the shared helper share a byte-identical
    L1 + L2 prefix and differ ONLY in the volatile tail.
(b) within-card item order is stable (by item id), independent of shuffled relevance scores.
Points (c) to (e), the providers' cache_control breakpoints, Gemini's system_instruction and the unchanged
plain-string calls, are tested with the providers in the hub (``tests/test_models.py``).

All offline: no network and no API key.
"""
from hubofhubs.recall.card_content_render import render_card_content_blocks
from quest_ai_runner.core.prompt_layers import (
    PromptLayers,
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
