"""Deadline-aware partial fuse in HybridContextAssembler.

When the caller passes ``meta["assembly_deadline"]`` (a ``time.monotonic()`` timestamp), an arm
that has not finished by the deadline is skipped and the completed arm(s) are fused as a PARTIAL
result (``AssembledContext.partial=True``) instead of blowing the caller's whole budget. If
NEITHER arm finished, the hybrid blocks for both exactly as before (the caller's own timeout +
late-recovery path owns the true zero-results case). Without a deadline, behavior is unchanged.
"""
import time

from quest_ai_runner.adapters.hybrid_context_assembler import HybridContextAssembler
from quest_ai_runner.core.adapters import AssembledContext


class _StubArm:
    """A ContextAssembler stub with a configurable delay and fixed output."""

    def __init__(self, view="", delay=0.0, card_metadata=None):
        self.view = view
        self.delay = delay
        self.card_metadata = card_metadata or []

    def assemble(self, task_text, *, meta=None):
        if self.delay:
            time.sleep(self.delay)
        return AssembledContext(
            context_view=self.view, card_metadata=list(self.card_metadata)
        )

    def record(self, task_text, outcome):
        pass


def _deadline_meta(seconds):
    return {"assembly_deadline": time.monotonic() + seconds}


def test_slow_vector_arm_skipped_returns_keyword_partial():
    kw = _StubArm(view="keyword content")
    vec = _StubArm(view="vector content", delay=0.6)
    hybrid = HybridContextAssembler(keyword=kw, vector=vec)
    t0 = time.monotonic()
    ac = hybrid.assemble("task", meta=_deadline_meta(0.1))
    elapsed = time.monotonic() - t0
    assert elapsed < 0.5, f"partial fuse must not wait out the slow arm (took {elapsed:.2f}s)"
    assert "keyword content" in ac.context_view
    assert "vector content" not in ac.context_view
    assert ac.partial is True


def test_slow_keyword_arm_skipped_returns_vector_partial():
    kw = _StubArm(view="keyword content", delay=0.6)
    vec = _StubArm(view="vector content")
    hybrid = HybridContextAssembler(keyword=kw, vector=vec)
    t0 = time.monotonic()
    ac = hybrid.assemble("task", meta=_deadline_meta(0.1))
    elapsed = time.monotonic() - t0
    assert elapsed < 0.5
    assert "vector content" in ac.context_view
    assert "keyword content" not in ac.context_view
    assert ac.partial is True


def test_both_arms_fast_within_deadline_full_fusion():
    kw = _StubArm(view="keyword content")
    vec = _StubArm(view="vector content")
    hybrid = HybridContextAssembler(keyword=kw, vector=vec)
    ac = hybrid.assemble("task", meta=_deadline_meta(5.0))
    assert "keyword content" in ac.context_view
    assert "vector content" in ac.context_view
    assert ac.partial is False


def test_no_deadline_waits_for_slow_arm_unchanged():
    """Without meta["assembly_deadline"] the prior blocking behavior is byte-for-byte kept."""
    kw = _StubArm(view="keyword content")
    vec = _StubArm(view="vector content", delay=0.3)
    hybrid = HybridContextAssembler(keyword=kw, vector=vec)
    ac = hybrid.assemble("task")
    assert "keyword content" in ac.context_view
    assert "vector content" in ac.context_view
    assert ac.partial is False


def test_neither_arm_finished_blocks_for_both():
    """An expired deadline with NOTHING completed must NOT return an early empty result: that
    would read as "assembly found nothing" and poison the caller's cache/late-recovery path.
    The hybrid blocks for both arms, exactly the pre-deadline behavior."""
    kw = _StubArm(view="keyword content", delay=0.3)
    vec = _StubArm(view="vector content", delay=0.3)
    hybrid = HybridContextAssembler(keyword=kw, vector=vec)
    t0 = time.monotonic()
    ac = hybrid.assemble("task", meta=_deadline_meta(0.05))
    elapsed = time.monotonic() - t0
    assert elapsed >= 0.25, "must have blocked for the arms rather than bailing out empty"
    assert "keyword content" in ac.context_view
    assert "vector content" in ac.context_view
    assert ac.partial is False


def _item_bearing_metadata(card_id="card-1"):
    return [{
        "id": card_id,
        "title": "Some card",
        "items": [{"id": "i1", "type": "note", "why": "w", "preview": "p", "text": "body"}],
    }]


def test_consolidation_skipped_on_partial_result(monkeypatch):
    """A partial fuse means the deadline already expired: the consolidating LLM pass is bypassed
    (fails never worse) and the mechanical partial merge is returned."""
    calls = []

    def fake_consolidate(*args, **kwargs):
        calls.append((args, kwargs))
        return None

    monkeypatch.setattr(
        "quest_ai_runner.core.card_filter.consolidate_context", fake_consolidate
    )
    kw = _StubArm(view="keyword content", card_metadata=_item_bearing_metadata())
    vec = _StubArm(view="vector content", delay=0.6)
    hybrid = HybridContextAssembler(keyword=kw, vector=vec, model_provider=object())
    ac = hybrid.assemble("task", meta=_deadline_meta(0.1))
    assert ac.partial is True
    assert calls == [], "consolidation must be bypassed when the result is partial"


def test_consolidation_skipped_when_budget_nearly_exhausted(monkeypatch):
    """Even a full fuse bypasses consolidation when the remaining budget could not absorb an
    LLM pass (< CONSOLIDATE_MIN_REMAINING_SECONDS left before the deadline)."""
    calls = []

    def fake_consolidate(*args, **kwargs):
        calls.append((args, kwargs))
        return None

    monkeypatch.setattr(
        "quest_ai_runner.core.card_filter.consolidate_context", fake_consolidate
    )
    kw = _StubArm(view="keyword content", card_metadata=_item_bearing_metadata())
    vec = _StubArm(view="vector content")
    hybrid = HybridContextAssembler(keyword=kw, vector=vec, model_provider=object())
    ac = hybrid.assemble("task", meta=_deadline_meta(0.2))  # both arms finish, little budget left
    assert ac.partial is False
    assert "keyword content" in ac.context_view
    assert calls == [], "consolidation must be bypassed when the remaining budget is too small"


def test_consolidation_still_runs_with_ample_budget(monkeypatch):
    """With a deadline far away, the consolidating pass engages exactly as without one."""
    calls = []

    def fake_consolidate(*args, **kwargs):
        calls.append((args, kwargs))
        return None  # None -> hybrid falls back to the mechanical merge

    monkeypatch.setattr(
        "quest_ai_runner.core.card_filter.consolidate_context", fake_consolidate
    )
    kw = _StubArm(view="keyword content", card_metadata=_item_bearing_metadata())
    vec = _StubArm(view="vector content")
    hybrid = HybridContextAssembler(keyword=kw, vector=vec, model_provider=object())
    ac = hybrid.assemble("task", meta=_deadline_meta(30.0))
    assert len(calls) == 1, "ample remaining budget must not suppress consolidation"
    assert "keyword content" in ac.context_view
