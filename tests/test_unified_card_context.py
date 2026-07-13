"""Unified context primitive (WS2): card/recent context reachable at EVERY loop step.

Covers the four behaviors the design requires (docs/HANDS_FREE_QUEST_AI_DESIGN.md sec. 3):
  (a) a mid-loop {"cards": query} read returns assembled card content via the SAME assembler;
  (b) a turn-start pre-fetch that TIMED OUT but completes late still serves a later mid-loop read
      from the shared cache -- with NO second assembly run;
  (c) the planner prompt + tool schema advertise the new card ops;
  (d) a failing / unsupported / absent card read returns a NAMED observation, never empty.
"""
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from typing import Any, Dict, List, Optional

from quest_ai_runner.core.adapters import AssembledContext
from quest_ai_runner.core.model_registry import ModelRegistry
from quest_ai_runner.core.orchestrator import (
    DECIDE_TOOL,
    Orchestrator,
    PLANNER_PROMPT,
    TurnCardCache,
)

from .conftest import StubProvider, StubRetrieval


def _orch(provider, retrieval, **kw):
    return Orchestrator(retrieval=retrieval, provider=provider,
                        registry=ModelRegistry(provider), **kw)


class StubAssembler:
    """A ContextAssembler whose assemble() returns a context_view built from the query, and which
    counts calls so a test can prove assembly ran (or did NOT re-run). Optionally supports
    render_card for the {"card": id} op, and can be made slow or made to raise."""

    def __init__(self, *, card_bodies: Optional[Dict[str, str]] = None,
                 delay: float = 0.0, raise_on_assemble: bool = False,
                 supports_render_card: bool = True):
        self.card_bodies = card_bodies or {}
        self.delay = delay
        self.raise_on_assemble = raise_on_assemble
        self.supports_render_card = supports_render_card
        self.assemble_calls: List[str] = []
        self.render_calls: List[str] = []

    def assemble(self, task_text: str, *, meta: Optional[Dict[str, Any]] = None,
                 on_event: Optional[Any] = None) -> AssembledContext:
        self.assemble_calls.append(task_text)
        if self.delay:
            time.sleep(self.delay)
        if self.raise_on_assemble:
            raise RuntimeError("assembler exploded")
        return AssembledContext(
            context_view=f"CARDFACTS::{task_text}::the answer is 42",
            card_metadata=[{"id": "card-1", "title": task_text}],
            sources=[{"adapter": "keyword", "label": "cards", "items": ["card-1"]}],
        )

    def record(self, task_text: str, outcome: Dict[str, Any]) -> None:
        pass

    # Present only when supports_render_card; the brain dispatches via getattr.
    def __getattr__(self, name):
        if name == "render_card" and self.__dict__.get("supports_render_card", True):
            def _render(card_id: str, *, meta: Optional[Dict[str, Any]] = None):
                self.render_calls.append(card_id)
                return self.card_bodies.get(card_id)
            return _render
        raise AttributeError(name)


# --- (a) mid-loop {"cards": query} returns assembled content ---------------------------------

def test_midloop_cards_read_returns_assembled_content():
    provider = StubProvider(decisions=[
        {"action": "read", "reads": [{"cards": "payroll schedule"}], "rationale": "recall topic"},
        {"action": "answer", "rationale": "have it"},
    ])
    assembler = StubAssembler()
    res = _orch(provider, StubRetrieval(), context_assembler=assembler).run("what about payroll?")

    assert res.kind == "answer"
    # The mid-loop cards read landed in gathered, rendered from the SAME assembler.
    cards_obs = [o for o in res.gathered
                 if isinstance(o, dict) and str(o.get("locator", "")).startswith("cards(")]
    assert cards_obs, f"no cards observation in gathered: {res.gathered}"
    assert "the answer is 42" in cards_obs[0]["text"]
    # The assembler was asked for exactly the query the planner requested.
    assert "payroll schedule" in assembler.assemble_calls


# --- (b) late pre-fetch is served from the shared cache with NO second assembly ---------------

def test_turn_start_timeout_then_late_prefetch_served_from_cache():
    # A slow assembler: the turn-start pre-fetch does not finish within the (tiny) turn-start
    # timeout, so it is "dropped" at turn start -- but the future keeps running.
    assembler = StubAssembler(delay=0.3)
    cache = TurnCardCache(assembler, meta=None)
    query = "quarterly goals"

    executor = ThreadPoolExecutor(max_workers=1)
    try:
        future = executor.submit(lambda: assembler.assemble(query, meta=None))
        cache.register_prefetch(query, future)

        # Turn start: collect with a tiny timeout -> it has NOT finished -> dropped, future KEPT.
        try:
            future.result(timeout=0.02)
            raised = False
        except FuturesTimeoutError:
            raised = True
        assert raised, "pre-fetch should not have finished within the turn-start window"

        # A later mid-loop read for the SAME query waits for the late future and serves it.
        assembled, origin = cache.assemble_for_query(query, timeout=5.0)
        assert assembled is not None
        assert "the answer is 42" in assembled.context_view
        assert origin == "prefetch"
        # The crux: assembly ran exactly ONCE (the pre-fetch), never a second time.
        assert assembler.assemble_calls == [query]

        # A repeat read is now a pure cache hit, still no new assembly.
        assembled2, origin2 = cache.assemble_for_query(query, timeout=5.0)
        assert origin2 == "cache"
        assert assembler.assemble_calls == [query]
    finally:
        executor.shutdown(wait=False)
        cache.close()


def test_fresh_midloop_query_runs_one_bounded_assemble():
    # No pre-fetch registered for this query -> assemble_for_query runs ONE fresh assemble, caches it.
    assembler = StubAssembler()
    cache = TurnCardCache(assembler, meta=None)
    try:
        assembled, origin = cache.assemble_for_query("brand new topic", timeout=5.0)
        assert origin == "fresh"
        assert "the answer is 42" in assembled.context_view
        assembled2, origin2 = cache.assemble_for_query("brand new topic", timeout=5.0)
        assert origin2 == "cache"
        assert assembler.assemble_calls == ["brand new topic"]  # exactly one assembly
    finally:
        cache.close()


# --- (b2) a PARTIAL result never displaces the full fuse in the shared cache ------------------

def test_partial_turn_start_discarded_midloop_read_assembles_full():
    # The orchestrator's collect contract for a PARTIAL turn-start result: use it for the
    # turn-start prompt, but discard the (already-done) prefetch future instead of registering
    # the partial as the query's completed result. The next same-query mid-loop read then falls
    # through to the FRESH assemble path (deadline-free meta) and recovers the FULL result.
    assembler = StubAssembler()
    cache = TurnCardCache(assembler, meta=None)
    query = "quarterly goals"

    executor = ThreadPoolExecutor(max_workers=1)
    try:
        partial = AssembledContext(context_view="PARTIAL VIEW", partial=True)
        future = executor.submit(lambda: partial)
        cache.register_prefetch(query, future)
        assert future.result(timeout=1.0) is partial  # turn-start collect harvested the partial

        cache.discard_prefetch(query)  # what the orchestrator does instead of register_result

        assembled, origin = cache.assemble_for_query(query, timeout=5.0)
        assert origin == "fresh", "the partial must not be served from the cache"
        assert "the answer is 42" in assembled.context_view
        assert assembler.assemble_calls == [query]

        # The recovered FULL result is cached normally.
        assembled2, origin2 = cache.assemble_for_query(query, timeout=5.0)
        assert origin2 == "cache"
        assert assembler.assemble_calls == [query]
    finally:
        executor.shutdown(wait=False)
        cache.close()


def test_late_partial_prefetch_served_once_but_never_cached():
    # Timeout branch: the deadline-bounded turn-start future stays registered and lands LATE
    # with a partial. A mid-loop read serves it (better than nothing) but must NOT cache it as
    # the completed result; the NEXT read assembles fresh and recovers the full fuse.
    assembler = StubAssembler()
    cache = TurnCardCache(assembler, meta=None)
    query = "quarterly goals"

    executor = ThreadPoolExecutor(max_workers=1)
    try:
        future = executor.submit(
            lambda: AssembledContext(context_view="PARTIAL VIEW", partial=True))
        cache.register_prefetch(query, future)

        assembled, origin = cache.assemble_for_query(query, timeout=5.0)
        assert origin == "prefetch"
        assert assembled.partial is True  # this read gets the late partial

        assembled2, origin2 = cache.assemble_for_query(query, timeout=5.0)
        assert origin2 == "fresh", "a cached partial would have short-circuited recovery"
        assert assembled2.partial is False
        assert "the answer is 42" in assembled2.context_view
        assert assembler.assemble_calls == [query]  # exactly one fresh assembly
    finally:
        executor.shutdown(wait=False)
        cache.close()


# --- (c) planner prompt + schema advertise the new ops ----------------------------------------

def test_planner_prompt_and_schema_mention_card_ops():
    # Prompt (formatted with the same slots _plan uses) advertises both ops.
    rendered = PLANNER_PROMPT.format(
        max_reads=6, max_subq=3, max_deep=3, mode_signal_block="",
        deferred_deep_semantics="", rationale_instruction="x",
        user_message="m", transcript="", context_view="", gathered="[]")
    assert '{"cards":' in rendered or '"cards"' in rendered
    assert '{"card":' in rendered or '"card"' in rendered
    assert "known-topic context" in rendered.lower()

    # Tool schema exposes both read-spec properties.
    read_props = DECIDE_TOOL["input_schema"]["properties"]["reads"]["items"]["properties"]
    assert "cards" in read_props
    assert "card" in read_props


# --- (d) failing / unsupported / absent card reads return NAMED observations ------------------

def test_cards_read_failure_yields_named_error_observation():
    provider = StubProvider(decisions=[{"action": "answer", "rationale": "x"}])
    assembler = StubAssembler(raise_on_assemble=True)
    orch = _orch(provider, StubRetrieval(), context_assembler=assembler)
    cache = TurnCardCache(assembler, meta=None)
    try:
        obs = orch.read_cards_context("anything", cache)
        assert obs.kind == "error"
        assert "anything" in (obs.locator or "")
        assert obs.error  # non-empty, names the failure
    finally:
        cache.close()


def test_cards_read_timeout_yields_named_error_observation(monkeypatch):
    monkeypatch.setenv("QAR_READ_OP_TIMEOUT_SECONDS", "0.05")
    provider = StubProvider(decisions=[{"action": "answer", "rationale": "x"}])
    assembler = StubAssembler(delay=1.0)  # slower than the op timeout
    orch = _orch(provider, StubRetrieval(), context_assembler=assembler)
    cache = TurnCardCache(assembler, meta=None)
    try:
        obs = orch.read_cards_context("slow topic", cache)
        assert obs.kind == "error"
        assert "timed out" in (obs.error or "").lower()
        assert "slow topic" in (obs.locator or "")
    finally:
        cache.close()


def test_card_read_present_absent_and_unsupported():
    provider = StubProvider(decisions=[{"action": "answer", "rationale": "x"}])

    # Present card -> content observation.
    assembler = StubAssembler(card_bodies={"card-7": "[card-7] Payroll\nruns on the 1st"})
    orch = _orch(provider, StubRetrieval(), context_assembler=assembler)
    cache = TurnCardCache(assembler, meta=None)
    try:
        obs = orch.read_one_card("card-7", cache)
        assert obs.kind == "query"
        assert "runs on the 1st" in (obs.text or "")

        # Absent card -> NAMED "no card" observation, never empty.
        obs_absent = orch.read_one_card("nope", cache)
        assert obs_absent.text and "nope" in obs_absent.text
    finally:
        cache.close()

    # An assembler WITHOUT render_card -> named "unsupported" observation.
    plain = StubAssembler(supports_render_card=False)
    orch2 = _orch(provider, StubRetrieval(), context_assembler=plain)
    cache2 = TurnCardCache(plain, meta=None)
    try:
        obs_unsupported = orch2.read_one_card("card-7", cache2)
        assert obs_unsupported.kind == "query"
        assert "does not support" in (obs_unsupported.text or "")
    finally:
        cache2.close()


def test_card_read_with_no_assembler_is_named_not_empty():
    provider = StubProvider(decisions=[{"action": "answer", "rationale": "x"}])
    orch = _orch(provider, StubRetrieval())  # no context_assembler wired
    obs = orch.read_cards_context("topic", None)
    assert obs.text and "unavailable" in obs.text.lower()
    obs2 = orch.read_one_card("card-1", None)
    assert obs2.text and "unavailable" in obs2.text.lower()
