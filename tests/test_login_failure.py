"""An invalid Claude login stops work once and says so once.

* the card index pass stops at its FIRST failing call (not one per chunk and per area), and an unread pass raises instead of reporting "no areas";
* a queued task whose run raises it is held to be tried again, never failed;
* the terminal's answer to a turn that raised it is the one sentence that says what to do, and an index pass paused by it says so in one line.
Offline: the provider is a stand-in that raises the typed error; nothing starts a CLI."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from hubofhubs.api import AuthenticationError
from quest_ai_runner.body.configuration.config_context_index import index_paused_note
from quest_ai_runner.consciousness.terminal.textual_ui_outcome import provider_error_lines
from quest_ai_runner.intelligence.subconscious.cards_from_files import context_topic_cards as topic_cards


class DiscoversThenFails:
    """Names three areas for the first call (discovery); every later call (topic extraction) raises the invalid-login error."""

    def __init__(self, fail_discovery=False):
        self.calls, self.fail_discovery = 0, fail_discovery

    def answer(self, messages, model=None, **kwargs):
        self.calls += 1
        if self.fail_discovery or self.calls > 1:
            raise AuthenticationError("claude CLI exited 1: Failed to authenticate: this login reached its time limit")
        files = [f"docs/area{i}/note{j}.md" for i in range(3) for j in range(3)]
        return json.dumps([{"name": f"Area {i}", "description": "d", "files": [f for f in files if f"area{i}/" in f]} for i in range(3)])


@pytest.fixture(autouse=True)
def one_worker(monkeypatch):
    monkeypatch.setattr(topic_cards, "bootstrap_workers", lambda: 1)       # one at a time, so the count of calls is exact


PATHS = [f"docs/area{i}/note{j}.md" for i in range(3) for j in range(3)]


def test_the_pass_stops_at_the_first_failing_extraction_not_one_per_area(tmp_path):
    provider = DiscoversThenFails()
    with pytest.raises(AuthenticationError):
        topic_cards._llm_topic_cards(PATHS, provider, model="haiku", walk_root=tmp_path)
    assert provider.calls == 2          # one discovery, the first extraction; the other two areas were never tried


def test_a_failed_discovery_is_unread_not_no_areas(tmp_path):
    provider = DiscoversThenFails(fail_discovery=True)
    with pytest.raises(AuthenticationError):
        topic_cards._llm_topic_cards(PATHS, provider, model="haiku", walk_root=tmp_path)
    assert provider.calls == 1


def test_an_ordinary_failure_still_degrades_to_no_cards(tmp_path):
    class Boom:
        def answer(self, *args, **kwargs):
            raise RuntimeError("API Error: 500")
    assert topic_cards._llm_topic_cards(PATHS, Boom(), model="haiku", walk_root=tmp_path) == []


def test_the_index_pause_note_names_the_cause_and_says_the_cards_are_unchanged():
    note = index_paused_note(AuthenticationError("x"))
    assert note.startswith("Context index paused: the Claude login is not valid.") and "unchanged" in note
    assert "." in note and "—" not in note


def test_the_terminal_answers_with_one_sentence_that_says_what_to_do():
    lines = provider_error_lines(AuthenticationError("claude CLI exited 1: Failed to authenticate"), message_restored=False)
    assert lines[0] == "Your Claude login is not valid, so I couldn't answer. Sign in again with /login in Claude Code."
    assert lines[1] == "Send your message again to try again after you sign in."
    wrapped = RuntimeError("planner failed")
    wrapped.__cause__ = AuthenticationError("x")
    assert provider_error_lines(wrapped, message_restored=True)[0].startswith("Your Claude login is not valid")


class RecordingClient:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        def record(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            if name == "requeue_for_provider_outage":
                return {"task_id": args[0], "status": "queued"}
            if name == "get_task":
                return {"status": "in_progress"}
            return {}
        return record

    def named(self, name):
        return [c for c in self.calls if c[0] == name]


class Raises:
    def __init__(self, error):
        self.error = error

    def run(self, *args, **kwargs):
        raise self.error


def test_a_queued_task_whose_run_raises_it_is_held_to_retry_not_failed():
    from quest_ai_runner.consciousness.request_intake.executor import TaskExecutor
    client = RecordingClient()
    outcome = TaskExecutor(client, Raises(AuthenticationError("Failed to authenticate"))).execute(
        {"task_id": "atask_7", "text": "draft the summary", "resume_session_id": "sess-1"})
    assert outcome.status == "waiting"
    (_, args, kwargs), = client.named("requeue_for_provider_outage")
    assert args == ("atask_7",) and kwargs["start_at"] > datetime.now(timezone.utc) + timedelta(minutes=5)
    assert kwargs["resume_session_id"] == "sess-1" and not client.named("report_failed")
    progress = [c for c in client.named("report_progress") if (c[2].get("data") or {}).get("phase") == "login_invalid"]
    assert progress and "not valid" in progress[0][2]["text"] and "—" not in progress[0][2]["text"]


def test_a_turn_whose_planner_hits_an_invalid_login_ends_with_the_typed_error_after_one_attempt():
    """The planner's failure used to fall back to a grounded answer, whose own call failed the same way and logged a second error. Now the turn ends once."""
    from quest_ai_runner.intelligence.mind.orchestrator import Orchestrator, OrchestratorConfig
    from quest_ai_runner.world.model_plumbing.model_tier_chooser.model_registry import ModelRegistry
    from .conftest import StubProvider, StubRetrieval

    class LoginProvider(StubProvider):
        def plan(self, prompt, *, model, tool_schema):
            self.plan_calls += 1
            raise AuthenticationError("claude CLI exited 1: Failed to authenticate")

    provider = LoginProvider(decisions=[])
    cfg = OrchestratorConfig()
    cfg.overseer = False
    orch = Orchestrator(retrieval=StubRetrieval({}), provider=provider, registry=ModelRegistry(provider), config=cfg)
    with pytest.raises(AuthenticationError):
        orch.run("What is the price?")
    assert provider.plan_calls == 1             # one planner attempt: no re-plan, no fallback after the typed error


class ReturnsUnstartedWorker:
    """The run comes back (nothing raised) with a deep worker the CLI refused to start because the login lapsed."""

    def run(self, *args, **kwargs):
        from types import SimpleNamespace
        deep = SimpleNamespace(met=False, launch_failed=True, launch_problem="", decision_id=None, usage_limited=False, output="",
                               error="The deep worker could not start: Failed to authenticate: OAuth session expired and could not be refreshed")
        return SimpleNamespace(kind="deep", text="", deep_results=[deep], session_id=None)


def test_a_worker_that_could_not_start_on_an_invalid_login_is_held_not_failed():
    from quest_ai_runner.consciousness.request_intake.executor import TaskExecutor
    client = RecordingClient()
    outcome = TaskExecutor(client, ReturnsUnstartedWorker()).execute({"task_id": "atask_9", "text": "improve the retention flow"})
    assert outcome.status == "waiting"
    assert client.named("requeue_for_provider_outage") and not client.named("report_failed")
