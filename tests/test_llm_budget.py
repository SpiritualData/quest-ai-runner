"""QAR's settings for the hub's call guard (env adapter, wiring, the structure watch's role) and the context index volume guard.
The counting, step-down and best rules are the hub's and are tested there."""
import json
from types import SimpleNamespace

import pytest
from hubofhubs.api import CallGuard, KeyedCallRefused, ModelNotAllowed

from quest_ai_runner.world.model_plumbing.spend import llm_budget as lb
from quest_ai_runner.world.model_plumbing.model_tier_chooser.call_roles import STRUCTURE_WATCH_CALL, tier_for_call
from quest_ai_runner.world.model_plumbing.vendor_model_caller import providers as prov
from quest_ai_runner.intelligence.subconscious.cards_from_files import index_llm_guard as guard

TOP = {"fast": "m-haiku", "balanced": "m-sonnet", "quality": "m-opus", "best": "m-fable"}


def test_defaults():
    limits = lb.limits_from_environment({})
    assert (limits.max_per_job, limits.max_per_hour, limits.best_requires_person, limits.keyless, limits.never_models) == (200, 600, True, False, ())


def test_env_names_map_to_limits():
    limits = lb.limits_from_environment({"QAR_MAX_LLM_CALLS_PER_JOB": "5", "QAR_MAX_LLM_CALLS_PER_HOUR": "off", "QAR_BEST_REQUIRES_USER_REQUEST": "0",
                                         "QAR_KEYLESS": "1", "QAR_NEVER_MODELS": "opus, fable"})
    assert (limits.max_per_job, limits.max_per_hour, limits.best_requires_person, limits.keyless) == (5, None, False, True)
    assert limits.never_models == ("opus", "fable")
    assert lb.limits_from_environment({"QAR_MAX_LLM_CALLS_PER_JOB": "junk"}).max_per_job == 200


def test_a_multi_provider_built_by_qar_carries_the_one_process_guard():
    class Api:
        pass
    router = prov.routed(providers={"anthropic": Api()})
    assert isinstance(router.call_guard, CallGuard) and router.call_guard is lb.guard()
    assert prov.routed().call_guard is router.call_guard


def test_uses_key_is_true_for_an_api_vendor_and_false_for_the_cli_path():
    lb.register_api_providers(["anthropic"])
    assert lb.uses_key("claude-sonnet-4") is True
    assert lb.uses_key("something-else") is False


def test_keyless_and_never_models_reach_the_guard_through_the_adapter():
    lb.register_api_providers(["anthropic"])
    keyless = CallGuard(lb.limits_from_environment({"QAR_KEYLESS": "1"}), uses_key=lb.uses_key)
    with pytest.raises(KeyedCallRefused):
        keyless.admit("claude-sonnet-4")
    never = CallGuard(lb.limits_from_environment({"QAR_NEVER_MODELS": "opus"}), uses_key=lb.uses_key)
    with pytest.raises(ModelNotAllowed):
        never.admit("claude-opus-9")


def test_mark_user_requested_best_lets_best_run_for_the_context():
    g = lb.guard()
    token = g.person_best.set(False)
    try:
        assert not g.best_allowed()
        lb.mark_user_requested_best()
        assert g.best_allowed()
    finally:
        g.person_best.reset(token)


def test_tier_for_call_marks_a_persons_best():
    g = lb.guard()
    token = g.person_best.set(False)
    try:
        assert tier_for_call("answer.response", "best") == "best" and g.best_allowed()
    finally:
        g.person_best.reset(token)


def test_structure_watch_asks_for_best_inside_its_role():
    from quest_ai_runner.intelligence.proactivity.autopilot_structure_watch import provider_ask
    seen = {}

    from hubofhubs.api import claude_cli

    class Provider(claude_cli.ClaudeCliProvider):      # a login lane: the hub names its best model
        def answer(self, messages, *, model, **kw):
            seen["role"] = lb.guard().call_role.get()
            seen["allowed"] = lb.guard().best_allowed()
            return "{}"

    provider_ask(Provider())("prompt", "best", 100)
    assert seen["role"] == "structure-watch" and seen["allowed"] is True
    assert lb.guard().call_role.get() == ""


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
