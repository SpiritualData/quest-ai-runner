"""Per-goal deep context + verifier-driven context-widening / tier-escalation iteration.

These cover the additive deep-loop behavior:
  * each deep goal selects its OWN context (assembler + conversation slice) per goal;
  * a not-met + need_more_context verdict triggers a SECOND iteration with MORE context at the
    requested tier;
  * a met-on-first-try goal does not iterate;
  * the single-goal / no-store / no-assembler path is unchanged.

All offline: a scripted provider replays planner decisions and verifier verdicts (told apart by the
tool schema name), and a stub deep runner records every call.
"""
from typing import Any, Dict, List, Optional

from quest_ai_runner.core.adapters import (
    AssembledContext,
    ConversationContext,
    DeepResult,
    Observation,
    RetrievalAdapterBase,
)
from quest_ai_runner.core.model_registry import ModelRegistry
from quest_ai_runner.core.orchestrator import Orchestrator, OrchestratorConfig

from .conftest import StubRetrieval


# --------------------------------------------------------------------------- stubs


class ScriptedProvider:
    """plan() replays PLANNER decisions and VERIFIER verdicts, told apart by tool schema name."""

    def __init__(self, plans: List[Dict[str, Any]], verdicts: List[Dict[str, Any]]):
        self._plans = list(plans)
        self._verdicts = list(verdicts)
        self.verify_calls = 0

    def plan(self, prompt: str, *, model: str, tool_schema: Dict[str, Any]) -> Dict[str, Any]:
        if tool_schema.get("name") == "goal_verdict":
            self.verify_calls += 1
            if self._verdicts:
                return self._verdicts.pop(0)
            return {"met": True}
        if self._plans:
            return self._plans.pop(0)
        return {"action": "answer", "rationale": "fallback", "model_tier": "sonnet"}

    def answer(self, messages, *, model, system=None) -> str:
        return "ANSWER"

    def list_models(self) -> List[str]:
        return ["claude-opus-4-8", "claude-sonnet-4-6", "claude-haiku-4-5"]


class RecordingRunner:
    """A DeepRunner that records every run_goal call and returns scripted results per call."""

    def __init__(self, results: Optional[List[DeepResult]] = None):
        self._results = list(results or [])
        self.calls: List[Dict[str, Any]] = []

    def run_goal(self, *, goal: str, brief: str, model: Optional[str] = None,
                 max_turns: Optional[int] = None,
                 context_preamble: Optional[str] = None) -> DeepResult:
        self.calls.append({"goal": goal, "brief": brief, "model": model,
                           "context_preamble": context_preamble})
        if self._results:
            return self._results.pop(0)
        return DeepResult(met=True, output="done")


class PerGoalAssembler:
    """A ContextAssembler that returns a context_view keyed by the task text, so each goal gets a
    distinct, identifiable per-goal context."""

    def __init__(self, mapping: Dict[str, str]):
        self.mapping = mapping
        self.assemble_calls: List[str] = []

    def assemble(self, task_text: str, *, meta: Optional[Dict[str, Any]] = None) -> AssembledContext:
        self.assemble_calls.append(task_text)
        for key, view in self.mapping.items():
            if key in (task_text or ""):
                return AssembledContext(context_view=view)
        return AssembledContext(context_view="")

    def record(self, task_text: str, outcome: Dict[str, Any]) -> None:
        return None


class WideningRetrieval(RetrievalAdapterBase):
    """A RetrievalAdapter whose grep returns a hit naming the query, so widening is observable.
    Inherits the benign discovery defaults so its presence does not pollute the planner's reads."""

    def __init__(self):
        self.grep_queries: List[str] = []

    def read_section(self, rel_path, *, start_line=None, end_line=None, heading=None, max_bytes=None):
        return Observation(kind="error", rel_path=rel_path, error="not found")

    def grep(self, pattern, *, scope=None, max_hits=None):
        self.grep_queries.append(pattern)
        return Observation(kind="grep", pattern=pattern,
                           hits=[{"rel_path": "found.py", "line_no": 7, "line": f"match for {pattern}"}])

    def query(self, spec):
        return Observation(kind="error", error="unsupported")


class StubConvStore:
    """A ConversationStore returning a distinct current-slice text per query."""

    def __init__(self):
        self.current_calls: List[str] = []
        self.related_calls: List[str] = []

    def current_slice(self, conv_id, query, *, recent_turns=4, max_chars=6000) -> ConversationContext:
        self.current_calls.append(query)
        return ConversationContext(text=f"conv-slice-for[{query[:30]}]")

    def related_slices(self, query, scope, *, exclude_conv_id=None, max_convs=3,
                       max_chars=6000) -> ConversationContext:
        self.related_calls.append(query)
        return ConversationContext(text=f"related-for[{query[:30]}]")


def _orch(provider, runner, *, retrieval=None, **kw):
    return Orchestrator(
        retrieval=retrieval if retrieval is not None else StubRetrieval({}),
        provider=provider,
        registry=ModelRegistry(provider),
        deep_runner=runner,
        config=OrchestratorConfig(deep_goal_max_iterations=3,
                                  deep_model_ladder=["haiku", "sonnet", "opus"]),
        **kw,
    )


# --------------------------------------------------------------------------- tests


def test_each_goal_gets_its_own_context():
    """Two deep subgoals each select DIFFERENT per-goal context from the assembler."""
    plan = {
        "action": "deep",
        "goal": "Overall goal",
        "deep_subtasks": [
            {"goal": "Fix the parser bug", "brief": "fix parser"},
            {"goal": "Add the export feature", "brief": "add export"},
        ],
        "rationale": "two pieces",
    }
    provider = ScriptedProvider(plans=[plan],
                                verdicts=[{"met": True}, {"met": True}])
    runner = RecordingRunner([DeepResult(met=True, output="parser fixed"),
                              DeepResult(met=True, output="export added")])
    assembler = PerGoalAssembler({
        "parser": "PARSER_CONTEXT lives here",
        "export": "EXPORT_CONTEXT lives here",
    })

    res = _orch(provider, runner, context_assembler=assembler).run("do both things")

    assert res.kind == "deep"
    assert len(runner.calls) == 2
    preambles = {c["goal"]: c["context_preamble"] for c in runner.calls}
    parser_pre = preambles["Fix the parser bug"]
    export_pre = preambles["Add the export feature"]
    assert "PARSER_CONTEXT" in parser_pre and "EXPORT_CONTEXT" not in parser_pre
    assert "EXPORT_CONTEXT" in export_pre and "PARSER_CONTEXT" not in export_pre


def test_not_met_need_more_context_iterates_with_more_context_at_requested_tier():
    """A not-met + need_more_context verdict triggers a 2nd iteration that has MORE context than the
    first, run at the verifier's requested tier."""
    plan = {"action": "deep", "goal": "Do the work",
            "deep_subtasks": [{"goal": "Implement feature X", "brief": "implement X"}],
            "rationale": "deep"}
    provider = ScriptedProvider(
        plans=[plan],
        verdicts=[{"met": False, "reason": "missing the config schema",
                   "need_more_context": True, "context_query": "config schema",
                   "next_tier": "opus", "next_action": "read the schema then finish"}],
    )
    # Attempt 1 falls short (error set so the loop does not trust worker success); attempt 2 succeeds.
    runner = RecordingRunner([DeepResult(met=False, output="partial", error="incomplete"),
                              DeepResult(met=True, output="done")])
    assembler = PerGoalAssembler({"config schema": "SCHEMA_CONTEXT for the missing piece",
                                  "Implement feature X": "BASE_CONTEXT"})
    retrieval = WideningRetrieval()

    res = _orch(provider, runner, context_assembler=assembler, retrieval=retrieval).run("build X")

    assert res.kind == "deep"
    assert len(runner.calls) == 2, "should have iterated a second time"
    first_pre = runner.calls[0]["context_preamble"] or ""
    second_pre = runner.calls[1]["context_preamble"] or ""
    # The second attempt has MORE context (the widened schema context the first lacked).
    assert "SCHEMA_CONTEXT" in second_pre
    assert "SCHEMA_CONTEXT" not in first_pre
    assert len(second_pre) > len(first_pre)
    # The widened query was used to grep the corpus.
    assert any("config schema" in q for q in retrieval.grep_queries)
    # The second attempt ran at the requested tier (opus resolves to a claude opus model id).
    assert "opus" in (runner.calls[1]["model"] or "")


def test_met_on_first_try_does_not_iterate():
    """A goal met on the first verification does not run a second worker iteration."""
    plan = {"action": "deep", "goal": "Quick fix",
            "deep_subtasks": [{"goal": "Rename the var", "brief": "rename"}],
            "rationale": "deep"}
    provider = ScriptedProvider(plans=[plan], verdicts=[{"met": True}])
    runner = RecordingRunner([DeepResult(met=True, output="renamed cleanly")])

    res = _orch(provider, runner).run("rename it")

    assert res.kind == "deep"
    assert len(runner.calls) == 1


def test_deferred_handoff_runs_once_and_is_not_reverified():
    """REGRESSION: an ASYNC HAND-OFF deep runner (e.g. a chat runner that queues a tracked task and
    returns a ``DeepResult(met=True, deferred=True, output="task #N launched")`` sentinel) must run
    EXACTLY ONCE. The goal loop must trust its ``met`` and stop, NOT re-verify the sentinel output
    against the goal — which always fails and would relaunch a fresh task every iteration (the
    runaway loop that spawned phantom tasks and left chat with no reply).

    The verifier is scripted to ALWAYS return not-met; before the fix the loop would re-verify and
    iterate up to deep_goal_max_iterations, calling run_goal that many times. After the fix it runs
    once and never verifies."""
    plan = {"action": "deep", "goal": "Confirm receipt",
            "deep_subtasks": [{"goal": "Confirm the message was received", "brief": "confirm"}],
            "rationale": "deep"}
    # Verdict would reject forever if it were ever consulted.
    provider = ScriptedProvider(plans=[plan], verdicts=[{"met": False, "reason": "sentinel"}])
    runner = RecordingRunner([
        DeepResult(met=True, output="task #2097 launched", deferred=True),
    ])

    res = _orch(provider, runner).run("confirm you got this")

    assert res.kind == "deep"
    assert len(runner.calls) == 1, "deferred hand-off must not relaunch a fresh task per iteration"
    assert provider.verify_calls == 0, "a deferred hand-off sentinel must not be re-verified"
    assert all(d.met for d in res.deep_results)


def test_single_goal_no_store_no_assembler_unchanged():
    """With no assembler and no store, a single-goal deep run carries NEITHER a per-goal context
    block NOR a widened-context block, so the new code adds nothing to the preamble (its presence is
    fully guarded on a wired adapter). It runs exactly once."""
    plan = {"action": "deep", "goal": "Just do it",
            "deep_subtasks": [{"goal": "Do it", "brief": "do it"}],
            "rationale": "deep"}
    provider = ScriptedProvider(plans=[plan], verdicts=[{"met": True}])
    runner = RecordingRunner([DeepResult(met=True, output="done")])

    res = _orch(provider, runner).run("do it")

    assert res.kind == "deep"
    assert len(runner.calls) == 1
    pre = runner.calls[0]["context_preamble"] or ""
    # None of the new per-goal / widening blocks leak in when nothing is wired.
    assert "CONTEXT SELECTED FOR THIS GOAL" not in pre
    assert "RELEVANT CONVERSATION FOR THIS GOAL" not in pre
    assert "ADDITIONAL CONTEXT" not in pre
    assert "RELATED CONVERSATIONS" not in pre


def test_per_goal_conversation_slice_included_when_store_wired():
    """When a conversation store and conv_id are in scope, each goal's preamble includes its own
    conversation slice."""
    plan = {"action": "deep", "goal": "Address it",
            "deep_subtasks": [{"goal": "Handle the migration", "brief": "migrate"}],
            "rationale": "deep"}
    provider = ScriptedProvider(plans=[plan], verdicts=[{"met": True}])
    runner = RecordingRunner([DeepResult(met=True, output="migrated")])
    store = StubConvStore()

    res = _orch(provider, runner, conversation_store=store).run(
        "do the migration", conv_id="conv-1", conv_scope={"user_id": "u1"})

    assert res.kind == "deep"
    pre = runner.calls[0]["context_preamble"] or ""
    assert "conv-slice-for" in pre
    assert any("Handle the migration" in q for q in store.current_calls)


# --------------------------------------------------------------------------- deep preamble: paste vs pointer


class ItemsAssembler:
    """A ContextAssembler whose AssembledContext carries structured ``card_metadata`` items, so the
    deep preamble can materialize each item paste-vs-pointer by its ``deliver`` tag."""

    def __init__(self, card_metadata: List[Dict[str, Any]], context_view: str = ""):
        self._cm = card_metadata
        self._view = context_view

    def assemble(self, task_text: str, *, meta: Optional[Dict[str, Any]] = None) -> AssembledContext:
        return AssembledContext(
            context_view=self._view,
            card_ids=[m["id"] for m in self._cm],
            card_metadata=[dict(m) for m in self._cm],
        )

    def record(self, task_text: str, outcome: Dict[str, Any]) -> None:
        return None


def test_deep_preamble_renders_pointer_for_file_and_pastes_others():
    """A file item tagged deliver=pointer becomes a POINTER line (the worker re-reads the file);
    a non-file item (and any deliver=paste item) is pasted VERBATIM."""
    plan = {"action": "deep", "goal": "Do the work",
            "deep_subtasks": [{"goal": "Implement feature X", "brief": "implement X"}],
            "rationale": "deep"}
    provider = ScriptedProvider(plans=[plan], verdicts=[{"met": True}])
    runner = RecordingRunner([DeepResult(met=True, output="done")])
    assembler = ItemsAssembler([
        {
            "id": "card-1", "title": "Feature X wiring", "adapter": "keyword",
            "items": [
                {"id": "f1", "type": "file", "why": "entry point",
                 "locator": {"path": "src/feature_x.py"},
                 "text": "FULL_FILE_TEXT_SHOULD_NOT_PASTE", "preview": "p",
                 "pointer_eligible": True, "deliver": "pointer"},
                {"id": "n1", "type": "note", "why": "rule",
                 "locator": {"text": "always validate X"},
                 "text": "PASTED_NOTE_TEXT", "preview": "p",
                 "pointer_eligible": False, "deliver": "paste"},
            ],
        },
    ])

    res = _orch(provider, runner, context_assembler=assembler).run("build X")

    assert res.kind == "deep"
    assert len(runner.calls) == 1
    pre = runner.calls[0]["context_preamble"] or ""
    # The file is delivered as a pointer (path named), NOT its full text.
    assert "src/feature_x.py" in pre
    assert "read this file fresh if needed" in pre
    assert "FULL_FILE_TEXT_SHOULD_NOT_PASTE" not in pre
    # The note is pasted verbatim.
    assert "PASTED_NOTE_TEXT" in pre
    # No em dashes leaked into the materialized pointer line.
    assert "—" not in pre


def test_deep_preamble_falls_back_to_context_view_when_no_items():
    """A file-only assembled context (no structured items) keeps using context_view verbatim."""
    plan = {"action": "deep", "goal": "Do the work",
            "deep_subtasks": [{"goal": "Implement feature X", "brief": "implement X"}],
            "rationale": "deep"}
    provider = ScriptedProvider(plans=[plan], verdicts=[{"met": True}])
    runner = RecordingRunner([DeepResult(met=True, output="done")])
    assembler = ItemsAssembler(
        [{"id": "c1", "title": "T", "adapter": "keyword"}],  # no items
        context_view="LEGACY_VIEW_TEXT",
    )

    _orch(provider, runner, context_assembler=assembler).run("build X")

    pre = runner.calls[0]["context_preamble"] or ""
    assert "LEGACY_VIEW_TEXT" in pre


def test_deep_preamble_pastes_rendered_section_and_swaps_pointer():
    """When a card carries a VERBATIM rendered_section, the deep preamble pastes it whole (summary +
    file listings survive), swapping only a pointer-delivered file item's fragment for a pointer
    line so the file body is not duplicated."""
    plan = {"action": "deep", "goal": "Do the work",
            "deep_subtasks": [{"goal": "Implement feature X", "brief": "implement X"}],
            "rationale": "deep"}
    provider = ScriptedProvider(plans=[plan], verdicts=[{"met": True}])
    runner = RecordingRunner([DeepResult(met=True, output="done")])
    # The file item's fragment in the section uses the same layout render_block_lines emits.
    frag_f1 = "  - (file) entry point\n      FULL_FILE_BODY_DO_NOT_PASTE"
    rendered_section = (
        "### Card: kw-a\nSUMMARY_A\n\nFiles:\n  - src/feature_x.py\n\nContent:\n" + frag_f1
    )
    assembler = ItemsAssembler([
        {
            "id": "kw-a", "title": "SUMMARY_A", "adapter": "keyword",
            "rendered_section": rendered_section,
            "items": [
                {"id": "f1", "type": "file", "why": "entry point",
                 "locator": {"path": "src/feature_x.py"},
                 "text": "FULL_FILE_BODY_DO_NOT_PASTE", "preview": "p",
                 "pointer_eligible": True, "deliver": "pointer"},
            ],
        },
    ])

    _orch(provider, runner, context_assembler=assembler).run("build X")

    pre = runner.calls[0]["context_preamble"] or ""
    # Summary + file listing from the verbatim section survive (the regression fix).
    assert "SUMMARY_A" in pre
    assert "src/feature_x.py" in pre
    # The pointer-delivered file's full body is NOT pasted; a pointer line replaces it.
    assert "FULL_FILE_BODY_DO_NOT_PASTE" not in pre
    assert "read this file fresh if needed" in pre
    assert "—" not in pre  # no em dashes leaked
