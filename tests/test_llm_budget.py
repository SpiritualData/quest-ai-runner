"""The call budget (per job, per hour), the step-down by volume, best-only-on-request, and the context index volume guard."""
import json
from types import SimpleNamespace

import pytest

from quest_ai_runner.world.model_plumbing.spend import llm_budget as lb
from quest_ai_runner.world.model_plumbing.model_tier_chooser.call_roles import STRUCTURE_WATCH_CALL, tier_for_call
from quest_ai_runner.world.model_plumbing.vendor_model_caller.providers import BudgetedMultiProvider
from quest_ai_runner.intelligence.subconscious.cards_from_files import index_llm_guard as guard

TOP = {"fast": "m-haiku", "balanced": "m-sonnet", "quality": "m-opus", "best": "m-fable"}


class Registry:
    def top_models(self):
        return dict(TOP)

    def resolve_tier(self, tier):
        return TOP[tier]

    def lower_tier(self, tier):
        return None


class Inner:
    tokens_in = tokens_out = 0

    def __init__(self):
        self.models = []

    def answer(self, messages, *, model, **kw):
        self.models.append(model)
        return "ok"


def make():
    inner = Inner()
    return BudgetedMultiProvider(inner, {}, None, Registry()), inner


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    lb.reset_for_tests()
    for name in ("QAR_MAX_LLM_CALLS_PER_JOB", "QAR_MAX_LLM_CALLS_PER_HOUR", "QAR_BEST_REQUIRES_USER_REQUEST"):
        monkeypatch.delenv(name, raising=False)
    yield
    lb.reset_for_tests()


def ask(provider, model):
    return provider.answer([{"role": "user", "content": "x"}], model=model)


def test_defaults():
    assert lb.max_calls_per_job() == 200 and lb.max_calls_per_hour() == 600 and lb.best_requires_user_request()


def test_job_budget_exceeded_raises_and_logs_once(monkeypatch, caplog):
    monkeypatch.setenv("QAR_MAX_LLM_CALLS_PER_JOB", "3")
    provider, inner = make()
    with lb.llm_job("context-index"), caplog.at_level("ERROR"):
        for _ in range(3):
            ask(provider, "m-haiku")
        for _ in range(2):
            with pytest.raises(lb.LlmBudgetExceeded):
                ask(provider, "m-haiku")
    assert len(inner.models) == 3
    lines = [r for r in caplog.records if "context-index" in r.getMessage()]
    assert len(lines) == 1 and "3" in lines[0].getMessage()


def test_hourly_budget_counts_calls_outside_any_job(monkeypatch):
    monkeypatch.setenv("QAR_MAX_LLM_CALLS_PER_HOUR", "2")
    provider, inner = make()
    ask(provider, "m-haiku")
    ask(provider, "m-haiku")
    with pytest.raises(lb.LlmBudgetExceeded):
        ask(provider, "m-haiku")
    assert len(inner.models) == 2


def test_hour_window_slides():
    lb.count_call(now=1000.0)
    assert len(lb._HOUR_CALLS) == 1
    lb.count_call(now=1000.0 + lb.HOUR + 1)
    assert len(lb._HOUR_CALLS) == 1


def test_step_down_after_20_calls_in_a_job_and_fast_stays_fast(caplog):
    provider, inner = make()
    with lb.llm_job("digest"), caplog.at_level("WARNING"):
        for _ in range(20):
            ask(provider, "m-opus")
        assert inner.models[-1] == "m-opus"           # the first 20 are untouched
        ask(provider, "m-opus")
        ask(provider, "m-fable")
        ask(provider, "m-haiku")
    assert inner.models[20:] == ["m-sonnet", "m-sonnet", "m-haiku"]
    assert len([r for r in caplog.records if "now run on" in r.getMessage()]) == 1


def test_a_call_outside_a_job_is_not_stepped_down():
    provider, inner = make()
    for _ in range(25):
        ask(provider, "m-opus")
    assert set(inner.models) == {"m-opus"}


def test_best_without_a_request_runs_on_quality_and_with_one_stays_best():
    provider, inner = make()
    ask(provider, "m-fable")
    assert inner.models == ["m-opus"]
    lb.mark_user_requested_best()
    ask(provider, "m-fable")
    assert inner.models[-1] == "m-fable"


def test_best_rule_can_be_lifted(monkeypatch):
    monkeypatch.setenv("QAR_BEST_REQUIRES_USER_REQUEST", "0")
    provider, inner = make()
    ask(provider, "m-fable")
    assert inner.models == ["m-fable"]


def test_tier_for_call_best_needs_a_person():
    assert tier_for_call("answer.response", "best") == "best" and lb.user_requested_best()
    lb.reset_for_tests()
    assert tier_for_call(STRUCTURE_WATCH_CALL) != "best"
    assert tier_for_call("answer.response") != "best"


def test_structure_watch_no_longer_asks_for_best():
    from quest_ai_runner.intelligence.proactivity.autopilot_structure_watch import provider_ask
    seen = {}

    class Provider:
        tokens_in = tokens_out = cost_usd = 0

        def answer(self, messages, *, model, **kw):
            seen["model"] = model
            return "{}"

        def list_models(self):
            return list(TOP.values())

        providers = {}

    provider_ask(Provider())("prompt", "best", 100)
    assert seen["model"] != TOP["best"]
    assert tier_for_call(STRUCTURE_WATCH_CALL) == "quality"


# ---- context index volume guard ----

def test_preflight_skips_over_the_file_limit_and_writes_a_marker(tmp_path, caplog):
    with caplog.at_level("WARNING"):
        assert guard.llm_allowed(tmp_path, 3001, 10) is False
        assert guard.llm_allowed(tmp_path, 3001, 10) is False          # the next scan: still skipped, no second warning
    assert len([r for r in caplog.records if "skipping every model stage" in r.getMessage()]) == 1
    assert json.loads((tmp_path / guard.MARKER_NAME).read_text())["files_to_analyse"] == 3001


def test_preflight_skips_over_the_card_limit_then_resumes_when_counts_drop(tmp_path):
    assert guard.llm_allowed(tmp_path, 5, 87190) is False
    assert (tmp_path / guard.MARKER_NAME).exists()
    assert guard.llm_allowed(tmp_path, 5, 2999) is True
    assert not (tmp_path / guard.MARKER_NAME).exists()


def test_per_run_caps_default_and_env(monkeypatch):
    files, stale = guard.cap_work([f"f{i}" for i in range(500)], [f"c{i}" for i in range(250)])
    assert len(files) == 300 and len(stale) == 100 and files[0] == "f0"
    monkeypatch.setenv("QAR_INDEX_MAX_NEW_FILES_PER_RUN", "7")
    monkeypatch.setenv("QAR_INDEX_MAX_STALE_CARDS_PER_RUN", "2")
    files, stale = guard.cap_work([f"f{i}" for i in range(500)], [f"c{i}" for i in range(250)])
    assert (len(files), len(stale)) == (7, 2)


class CountingProvider:
    def __init__(self):
        self.calls = 0


def test_walk_makes_no_model_call_when_preflight_skips(tmp_path, monkeypatch):
    from quest_ai_runner.body.internal_context.card_store.file_context_store import FileContextStore
    import quest_ai_runner.intelligence.subconscious.cards_from_files.context_store_bootstrap_walk as walk
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    for i in range(5):
        (corpus / f"f{i}.md").write_text(f"# file {i}\n")
    monkeypatch.setenv("QAR_INDEX_LLM_MAX_FILES", "2")
    called = []
    monkeypatch.setattr(walk, "propose_topic_cards", lambda *a, **k: called.append(1) or [])
    monkeypatch.setattr(walk, "folder_gate", lambda *a, **k: SimpleNamespace(excluded=set(), **{}), raising=False)
    store = FileContextStore(cards_dir=str(tmp_path / "cards"), repo_root=str(corpus), auto_bootstrap=False)
    store.bootstrap(root=str(corpus), provider=object(), model="m-haiku")
    assert called == []
    assert (tmp_path / "cards" / guard.MARKER_NAME).exists()
