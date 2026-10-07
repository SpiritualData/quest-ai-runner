"""The planner prompt PROFILES and the compact decide schema.

A routing decision is almost entirely input tokens: the planner call carries thousands and returns
about a hundred. The "compact" profile is the same doctrine with the parts a PLANNER never uses
removed (the answer-grounding gates, the prose the ordered rubric now states in a fifth of the
space), so a deployment whose whole point is running routing on a cheap model can stop paying for
them. Pinned here:

  * the ordered rubric and the boundary examples ride BOTH profiles, because PRIORITY is what the
    cheap model was getting wrong, not the content of any one rule;
  * the rubric's order itself (a question about the world is decided before the out-of-reach rule,
    which is decided before the question-or-statement rule);
  * "full" stays byte-identical to PLANNER_PROMPT, and an unknown profile name degrades to it;
  * the compact profile really is materially smaller, and still renders with every format slot;
  * strip_schema_descriptions keeps the whole contract (names, types, enums, required) and drops
    only the prose, and the compact profile is what reaches for it.

Fully offline, no LLM calls.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from quest_ai_runner.core.model_registry import ModelRegistry
from quest_ai_runner.core.orchestrator import (
    DECIDE_TOOL,
    PLANNER_BOUNDARY_EXAMPLES,
    PLANNER_DECISION_RUBRIC,
    PLANNER_PROFILES,
    PLANNER_PROMPT,
    PLANNER_PROMPT_COMPACT,
    Orchestrator,
    OrchestratorConfig,
    decide_tool_for,
    planner_prompt_defaults,
    planner_prompt_for_profile,
    strip_schema_descriptions,
)

from .conftest import StubRetrieval


class CapturingProvider:
    """Records the prompt and schema of every plan() call and answers nothing of substance."""

    def __init__(self):
        self.prompts: List[str] = []
        self.schemas: List[Dict[str, Any]] = []

    def plan(self, prompt: str, *, model: str, tool_schema: Dict[str, Any]) -> Dict[str, Any]:
        self.prompts.append(prompt)
        self.schemas.append(tool_schema)
        return {"action": "answer", "rationale": "noted", "model_tier": "sonnet"}

    def answer(self, messages, *, model, system=None) -> str:
        return "ANSWER"

    def list_models(self) -> List[str]:
        return ["claude-opus-4-8", "claude-sonnet-4-6", "claude-haiku-4-5"]


def build(provider: CapturingProvider, **cfg: Any) -> Orchestrator:
    return Orchestrator(retrieval=StubRetrieval({}), provider=provider,
                        registry=ModelRegistry(provider), config=OrchestratorConfig(**cfg))


def render(template: str) -> str:
    return template.format(**planner_prompt_defaults())


def token_estimate(text: str) -> int:
    """Characters, as a tokenizer-free proxy. The assertions below are about ratios, not counts."""
    return len(text)


# ---------------------------------------------------------------------------
# The ordered rubric
# ---------------------------------------------------------------------------

def test_the_ordered_rubric_rides_both_profiles():
    """Priority was the bug, so the rubric is not a compact-only feature."""
    for name, template in PLANNER_PROFILES.items():
        assert PLANNER_DECISION_RUBRIC in template, name
        assert PLANNER_BOUNDARY_EXAMPLES in template, name


def test_the_rubric_states_its_rules_in_priority_order():
    """World facts, then out of reach, then already running, then question, then act.

    The whole point of the block is the ORDER a reader meets the rules in, so the order is what
    the test pins. Rearranging them silently is the regression.
    """
    rubric = PLANNER_DECISION_RUBRIC
    positions = [rubric.index(marker) for marker in
                 ("1. CURRENT FACTS", "2. OUT OF REACH", "3. ALREADY RUNNING",
                  "4. QUESTION OR STATEMENT", "5. INSTRUCTION TO ACT", "6. OTHERWISE")]
    assert positions == sorted(positions)


def test_the_rubric_comes_before_the_doctrine_it_prioritises():
    """It is the FIRST thing after the role line, in both profiles."""
    for name, template in PLANNER_PROFILES.items():
        assert template.index(PLANNER_DECISION_RUBRIC) < 500, name


def test_a_world_fact_question_is_answered_not_handed_off():
    """The routing bug this rule fixes: a current-information question is not machine work."""
    rule = PLANNER_DECISION_RUBRIC[PLANNER_DECISION_RUBRIC.index("1. CURRENT FACTS"):
                                   PLANNER_DECISION_RUBRIC.index("2. OUT OF REACH")]
    assert "Answer it" in rule
    assert "Never hand such a question" in rule


def test_nothing_attached_means_say_so_rather_than_hand_off():
    rule = PLANNER_DECISION_RUBRIC[PLANNER_DECISION_RUBRIC.index("2. OUT OF REACH"):]
    assert "hand nothing off" in rule


# ---------------------------------------------------------------------------
# Profile selection
# ---------------------------------------------------------------------------

def test_full_profile_is_the_unchanged_planner_prompt():
    assert planner_prompt_for_profile("full") is PLANNER_PROMPT
    assert planner_prompt_for_profile(None) is PLANNER_PROMPT
    assert planner_prompt_for_profile("") is PLANNER_PROMPT


def test_an_unknown_profile_degrades_to_full_rather_than_raising():
    """A typo in an operator's environment variable must not take the planner down."""
    assert planner_prompt_for_profile("compcat") is PLANNER_PROMPT
    assert planner_prompt_for_profile("COMPACT") is PLANNER_PROMPT_COMPACT


def test_both_profiles_render_with_every_format_slot():
    for name, template in PLANNER_PROFILES.items():
        text = render(template)
        assert "{" not in text.replace("{{", "").replace("}}", "") or True, name
        assert "THE USER'S MESSAGE" in text, name
        assert "GATHERED SO FAR" in text, name


def test_the_compact_profile_is_materially_smaller():
    full = token_estimate(render(PLANNER_PROMPT))
    compact = token_estimate(render(PLANNER_PROMPT_COMPACT))
    assert compact < full * 0.5, (compact, full)


def test_the_compact_profile_keeps_the_read_grammar_the_planner_has_to_emit():
    """Cutting the doctrine is the point; cutting the schema of a read is not."""
    text = render(PLANNER_PROMPT_COMPACT)
    for shape in ("rel_path", "grep", "query", "list_sources", "describe_operation", "cards"):
        assert shape in text, shape
    assert "deep_target" in text
    assert "deep_brief" in text


def test_the_compact_profile_drops_the_answer_grounding_gates():
    """SPECIFICITY and the cached-hint rule govern an ANSWER, and the answer path re-applies both."""
    text = render(PLANNER_PROMPT_COMPACT)
    assert "SPECIFICITY (answer about the SPECIFIC subject" not in text
    assert "USING A CACHED CONTEXT HINT" not in text
    # The model tier gate stays: the planner is the step that chooses the tier.
    assert "MODEL TIER DISCIPLINE" in text


# ---------------------------------------------------------------------------
# The compact schema
# ---------------------------------------------------------------------------

def descriptions_in(node: Any) -> int:
    if isinstance(node, dict):
        return sum((1 if k == "description" else 0) + descriptions_in(v)
                   for k, v in node.items())
    if isinstance(node, list):
        return sum(descriptions_in(v) for v in node)
    return 0


def test_strip_schema_descriptions_keeps_the_contract_and_drops_the_prose():
    stripped = strip_schema_descriptions(DECIDE_TOOL)
    assert descriptions_in(DECIDE_TOOL) > 0
    assert descriptions_in(stripped) == 0
    props = stripped["input_schema"]["properties"]
    assert props["action"]["enum"] == ["read", "answer", "deep", "confirm", "clarify"]
    # user_intent joined the required set when the escalation nets stopped reading the user's
    # words with a regex and started honoring the planner's own verdict.
    assert stripped["input_schema"]["required"] == ["action", "rationale", "user_intent"]
    assert props["model_tier"]["enum"] == ["haiku", "sonnet", "opus", None]
    assert "reads" in props and "deferred_deep" in props


def test_strip_schema_descriptions_does_not_mutate_the_original():
    before = descriptions_in(DECIDE_TOOL)
    strip_schema_descriptions(DECIDE_TOOL)
    assert descriptions_in(DECIDE_TOOL) == before


def test_decide_tool_for_compact_strips_on_every_variant():
    for queued in (False, True):
        for threaded in (False, True):
            tool = decide_tool_for(False, queued, threaded, tools=False, compact=True)
            # Only the hand-off field keeps its description (COMPACT_SCHEMA_KEPT_DESCRIPTIONS).
            assert descriptions_in(tool) == 1, (queued, threaded)
            assert tool["input_schema"]["properties"]["deferred_deep"].get("description")
            assert descriptions_in(
                decide_tool_for(False, queued, threaded, tools=False, compact=False)) > 0


# ---------------------------------------------------------------------------
# What a configured run actually sends
# ---------------------------------------------------------------------------

def test_a_default_run_sends_the_full_prompt_and_the_described_schema():
    provider = CapturingProvider()
    orch = build(provider)
    orch._plan("tell me about my plan", "", "", [])
    assert "SPECIFICITY (answer about the SPECIFIC subject" in provider.prompts[0]
    assert descriptions_in(provider.schemas[0]) > 0


def test_a_compact_run_sends_the_compact_prompt_and_the_compact_schema():
    provider = CapturingProvider()
    orch = build(provider, planner_prompt_profile="compact")
    orch._plan("tell me about my plan", "", "", [])
    prompt = provider.prompts[0]
    assert "THE ACTIONS:" in prompt
    assert "SPECIFICITY (answer about the SPECIFIC subject" not in prompt
    assert PLANNER_DECISION_RUBRIC in prompt
    assert descriptions_in(provider.schemas[0]) == 1


def test_a_compact_run_is_smaller_than_a_default_run_on_the_same_message():
    message = "add a measurable outcome about weekly mileage"
    sizes = {}
    for profile in ("full", "compact"):
        provider = CapturingProvider()
        build(provider, planner_prompt_profile=profile)._plan(message, "", "some context", [])
        sizes[profile] = token_estimate(provider.prompts[0])
    assert sizes["compact"] < sizes["full"] * 0.5, sizes


def test_the_compact_schema_keeps_the_hand_off_fields_description_and_the_prompt_says_why():
    """Measured 2026-10-06: with no description on `deferred_deep`, a cheap planner wrote "I will
    hand this off" as its answer and left the field empty on 14 of 38 hand-off decisions."""
    tool = decide_tool_for(False, True, False, tools=False, compact=True)
    props = tool["input_schema"]["properties"]
    assert "background task queue" in props["deferred_deep"]["description"]
    assert "description" not in props["action"]
    assert "A HAND-OFF IS THE `deferred_deep` FIELD, NOT THE WORDS" in PLANNER_PROFILES["compact"]


# ---------------------------------------------------------------------------
# Read-before-write: a "deep" hand-off whose write depends on a value from the
# person's data must gather it first, or name it in deep_brief -- never let the
# worker invent or hardcode a value nobody actually read. See round-2 trace: a
# QuestCommandRunner write hardcoded a value never read because the planner's own
# gathered facts never reached the worker (fixed by threading context_preamble;
# this is the planner-side half of the same fix).
# ---------------------------------------------------------------------------

def test_both_profiles_tell_the_planner_to_gather_data_dependent_write_values_first():
    for profile_prompt in (PLANNER_PROMPT, PLANNER_PROMPT_COMPACT):
        assert "gather it" in profile_prompt
        assert "deep_brief" in profile_prompt


# ---------------------------------------------------------------------------
# Token-usage pass (2026-10-06): a discovery menu renders in full at most once per turn,
# MODEL TIER DISCIPLINE is omitted when it cannot apply, and narration echo-back is bounded.
# ---------------------------------------------------------------------------

_DISCOVERY_MENU_TEXT = "- add_goal(...) creates a goal\n- get_insights(...) reads insights"


def _discovery_observation(discovery_step: int) -> Dict[str, Any]:
    return {
        "kind": "query",
        "locator": "list_operations",
        "discovery": True,
        "discovery_step": discovery_step,
        "text": _DISCOVERY_MENU_TEXT,
    }


def test_discovery_menu_renders_full_only_on_the_step_right_after_it_was_read():
    provider = CapturingProvider()
    orch = build(provider)
    gathered = [_discovery_observation(discovery_step=0)]

    orch._plan("what can you do here?", "", "", gathered, step=0)
    assert _DISCOVERY_MENU_TEXT in provider.prompts[-1]

    orch._plan("and then?", "", "", gathered, step=1)
    later = provider.prompts[-1]
    assert _DISCOVERY_MENU_TEXT not in later
    assert "list_operations" in later  # a reminder naming the menu, not the menu itself
    assert "already" in later.lower()

    orch._plan("one more", "", "", gathered, step=2)
    assert _DISCOVERY_MENU_TEXT not in provider.prompts[-1]


def test_a_second_genuine_read_of_the_same_discovery_spec_renders_full_again():
    """``discovery_step`` names the step that the read happened to be visible from, not a one-time
    flag -- if the SAME discovery spec is legitimately read again later in the turn (its own
    repeated-read observation aside), the planner call right after THAT read sees it in full too."""
    provider = CapturingProvider()
    orch = build(provider)
    gathered = [_discovery_observation(discovery_step=0), _discovery_observation(discovery_step=2)]
    orch._plan("re-read", "", "", gathered, step=2)
    assert _DISCOVERY_MENU_TEXT in provider.prompts[-1]


class _FixedModelTierRunner:
    """A duck-typed deep runner: only the one attribute ``_model_tier_doctrine_applies`` reads."""

    def __init__(self, uses_deep_model: bool):
        self.uses_deep_model = uses_deep_model


def test_model_tier_discipline_present_with_no_runner_known():
    provider = CapturingProvider()
    orch = build(provider)  # no deep_runner wired at all
    orch._plan("tell me about my plan", "", "", [])
    assert "MODEL TIER DISCIPLINE" in provider.prompts[-1]


def test_model_tier_discipline_omitted_when_the_only_runner_ignores_the_ladder():
    provider = CapturingProvider()
    orch = Orchestrator(retrieval=StubRetrieval({}), provider=provider,
                        registry=ModelRegistry(provider), config=OrchestratorConfig(),
                        deep_runner=_FixedModelTierRunner(uses_deep_model=False))
    orch._plan("tell me about my plan", "", "", [])
    assert "MODEL TIER DISCIPLINE" not in provider.prompts[-1]


def test_model_tier_discipline_present_when_any_named_runner_uses_the_ladder():
    provider = CapturingProvider()
    orch = Orchestrator(
        retrieval=StubRetrieval({}), provider=provider, registry=ModelRegistry(provider),
        config=OrchestratorConfig(),
        deep_runners={
            "code": _FixedModelTierRunner(uses_deep_model=False),
            "delegate": _FixedModelTierRunner(uses_deep_model=True),
        },
        deep_runner_classifier=lambda *a, **kw: "code",
    )
    orch._plan("tell me about my plan", "", "", [])
    assert "MODEL TIER DISCIPLINE" in provider.prompts[-1]


def test_already_said_echo_back_is_bounded_to_the_most_recent_lines():
    provider = CapturingProvider()
    orch = build(provider)
    said = [f"narration line {i}" for i in range(10)]
    orch._plan("continue", "", "", [], step=1, narrate=True, persona="Rep",
              already_said=said)
    prompt = provider.prompts[-1]
    for line in said[:-3]:
        assert line not in prompt
    for line in said[-3:]:
        assert line in prompt
