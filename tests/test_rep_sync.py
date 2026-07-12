"""rep_sync — pull/push an AI rep's profile <-> its Claude skill file, against a MOCK client.

No network: a tiny in-memory Quest client records get/update calls and serves a canned profile.
"""
import tempfile
from pathlib import Path

import pytest

from quest_ai_runner.runner.rep_sync import (
    RepSyncError,
    parse_skill_file,
    pull_rep_to_skill,
    push_skill_to_rep,
    render_skill_file,
    sync_rep,
)


class MockProfileClient:
    """In-memory stand-in for QuestClient's AI-profile surface. Records every call; no HTTP."""

    def __init__(self, profile=None):
        self._profile = profile or {
            "user_id": "u1",
            "display_name": "Rep One",
            "persona": "You are decisive and concise.",
            "learned_notes": [
                {"id": "note_1", "text": "be concise in status updates", "created_at": "2026-01-01"},
                {"id": "note_2", "text": "never schedule meetings on Fridays"},
            ],
            "updated_at": "2026-01-02",
        }
        self.get_calls = []
        self.updates = []   # list of dicts of kwargs passed to update_ai_profile

    def get_ai_profile(self, user_id, *, team_id=None):
        self.get_calls.append((team_id, user_id))
        return dict(self._profile)

    def update_ai_profile(self, user_id, *, display_name=None, persona=None,
                          learned_notes=None, team_id=None):
        self.updates.append({
            "team_id": team_id, "user_id": user_id, "display_name": display_name,
            "persona": persona, "learned_notes": learned_notes,
        })
        return dict(self._profile)


# --- rendering / parsing round-trip ---------------------------------------------

def test_render_then_parse_round_trips_persona_and_notes():
    profile = {
        "persona": "Be kind. Be brief.",
        "learned_notes": [{"id": "a", "text": "x"}, {"id": "b", "text": "y"}],
    }
    text = render_skill_file("", profile)
    parsed = parse_skill_file(text)
    assert parsed["persona"] == "Be kind. Be brief."
    assert parsed["learned_notes"] == [{"id": "a", "text": "x"}, {"id": "b", "text": "y"}]


def test_render_preserves_human_authored_content():
    existing = "# My Rep\n\nHuman-written intro that must survive.\n"
    out = render_skill_file(existing, {"persona": "P", "learned_notes": []})
    assert "Human-written intro that must survive." in out
    assert "QAR:MANAGED:persona" in out


def test_render_is_idempotent():
    profile = {"persona": "P", "learned_notes": [{"id": "1", "text": "t"}]}
    once = render_skill_file("# Head\n", profile)
    twice = render_skill_file(once, profile)
    assert once == twice


def test_render_overwrites_only_managed_region_on_change():
    profile1 = {"persona": "OLD", "learned_notes": []}
    profile2 = {"persona": "NEW", "learned_notes": []}
    v1 = render_skill_file("# Keep me\n", profile1)
    v2 = render_skill_file(v1, profile2)
    assert "NEW" in v2 and "OLD" not in v2
    assert "# Keep me" in v2


def test_parse_skips_placeholder_and_ignores_unmarked_bullets():
    profile = {"persona": "P", "learned_notes": []}
    text = render_skill_file("- this bullet is OUTSIDE managed blocks\n", profile)
    parsed = parse_skill_file(text)
    assert parsed["learned_notes"] == []          # placeholder + outside bullet both excluded


# --- pull -----------------------------------------------------------------------

def test_pull_writes_skill_file_with_profile_data():
    client = MockProfileClient()
    with tempfile.TemporaryDirectory() as d:
        res = pull_rep_to_skill(client, "team1", "u1", d)
        content = Path(d, "SKILL.md").read_text()
    assert client.get_calls == [("team1", "u1")]
    assert res.pulled and res.direction == "pull"
    assert res.learned_count == 2
    assert "You are decisive and concise." in content
    assert "be concise in status updates" in content
    assert "<!-- id:note_1 -->" in content


def test_pull_is_idempotent_no_rewrite_when_unchanged():
    client = MockProfileClient()
    with tempfile.TemporaryDirectory() as d:
        pull_rep_to_skill(client, "team1", "u1", d)
        first = Path(d, "SKILL.md").read_text()
        pull_rep_to_skill(client, "team1", "u1", d)
        second = Path(d, "SKILL.md").read_text()
    assert first == second


def test_pull_empty_profile_raises_and_leaves_file_untouched():
    """A failed/missing profile ({} from get_ai_profile) must never blank the local skill file.

    QuestClient.get_ai_profile returns {} when the GET fails or no rep exists; rendering that
    would wipe the managed persona locally, and a later "both"-direction push would write the
    wipe up to Quest. pull_rep_to_skill refuses instead.
    """
    client = MockProfileClient()
    client._profile = {}  # constructor treats {} as "use the default", so set it directly
    with tempfile.TemporaryDirectory() as d:
        existing = render_skill_file("# Keep\n", {"persona": "SEEDED", "learned_notes": []})
        Path(d, "SKILL.md").write_text(existing)
        with pytest.raises(RepSyncError):
            pull_rep_to_skill(client, "team1", "u1", d)
        assert Path(d, "SKILL.md").read_text() == existing  # byte-identical, nothing clobbered


def test_pull_preserves_existing_human_content():
    client = MockProfileClient()
    with tempfile.TemporaryDirectory() as d:
        Path(d, "SKILL.md").write_text("# Hand-written header\n\nKeep this.\n")
        pull_rep_to_skill(client, "team1", "u1", d)
        content = Path(d, "SKILL.md").read_text()
    assert "Keep this." in content
    assert "You are decisive and concise." in content


# --- push -----------------------------------------------------------------------

def test_push_reads_skill_file_and_puts_profile():
    client = MockProfileClient()
    with tempfile.TemporaryDirectory() as d:
        pull_rep_to_skill(client, "team1", "u1", d)    # render a valid file first
        res = push_skill_to_rep(client, "team1", "u1", d)
    assert res.pushed and res.direction == "push"
    assert len(client.updates) == 1
    up = client.updates[0]
    assert up["team_id"] == "team1" and up["user_id"] == "u1"
    assert up["persona"] == "You are decisive and concise."
    texts = [n["text"] for n in up["learned_notes"]]
    assert "never schedule meetings on Fridays" in texts
    # ids round-trip back up
    assert {n.get("id") for n in up["learned_notes"]} == {"note_1", "note_2"}


def test_push_picks_up_a_local_edit():
    client = MockProfileClient()
    with tempfile.TemporaryDirectory() as d:
        pull_rep_to_skill(client, "team1", "u1", d)
        p = Path(d, "SKILL.md")
        p.write_text(p.read_text().replace("decisive and concise", "warm and thorough"))
        push_skill_to_rep(client, "team1", "u1", d)
    assert client.updates[0]["persona"] == "You are warm and thorough."


def test_push_missing_file_raises():
    client = MockProfileClient()
    with tempfile.TemporaryDirectory() as d:
        with pytest.raises(RepSyncError):
            push_skill_to_rep(client, "team1", "u1", d)


# --- the one entry point --------------------------------------------------------

def test_sync_rep_pull_default():
    client = MockProfileClient()
    with tempfile.TemporaryDirectory() as d:
        res = sync_rep(client, "team1", "u1", d)
    assert res.direction == "pull" and res.pulled and not res.pushed


def test_sync_rep_push():
    client = MockProfileClient()
    with tempfile.TemporaryDirectory() as d:
        pull_rep_to_skill(client, "team1", "u1", d)
        res = sync_rep(client, "team1", "u1", d, direction="push")
    assert res.direction == "push" and res.pushed


def test_sync_rep_both_pulls_then_pushes():
    client = MockProfileClient()
    with tempfile.TemporaryDirectory() as d:
        res = sync_rep(client, "team1", "u1", d, direction="both")
    assert res.pulled and res.pushed and res.direction == "both"
    assert client.get_calls and client.updates       # both sides exercised


def test_sync_rep_unknown_direction_raises():
    client = MockProfileClient()
    with tempfile.TemporaryDirectory() as d:
        with pytest.raises(ValueError):
            sync_rep(client, "team1", "u1", d, direction="sideways")


# --- QuestClient AI-profile endpoint shaping (no network) -----------------------

def test_quest_client_ai_profile_endpoints_shape_requests():
    from quest_ai_runner.runner.quest_client import QuestClient

    calls = []

    class CapturingClient(QuestClient):
        def _request(self, method, path, *, params=None, body=None):
            calls.append((method, path, params, body))
            return {"ok": True}

    c = CapturingClient("http://x", "qsk_test", team_id="team1")
    c.get_ai_profile("u1")
    c.update_ai_profile("u1", persona="P", learned_notes=[{"text": "t"}])
    c.add_rep_correction("u1", "be brief", message_id="m9")

    assert calls[0] == ("GET", "/api/teams/team1/members/u1/ai-profile", None, None)
    assert calls[1][:2] == ("PUT", "/api/teams/team1/members/u1/ai-profile")
    assert calls[1][3] == {"persona": "P", "learned_notes": [{"text": "t"}]}   # display_name omitted
    assert calls[2][:2] == ("POST", "/api/teams/team1/members/u1/corrections")
    assert calls[2][3] == {"correction": "be brief", "message_id": "m9"}


def test_quest_client_ai_profile_requires_team_id():
    from quest_ai_runner.runner.quest_client import QuestClient, QuestNotConfigured

    c = QuestClient("http://x", "qsk_test")   # no team_id
    with pytest.raises(QuestNotConfigured):
        c.get_ai_profile("u1")


# --- poller integration: opt-in pull-before-run hook ----------------------------

def _profile_aware_mock_client(profile_client):
    """A MockQuestClient (task surface) that also serves the AI-profile surface for the hook."""
    from .test_runner import MockQuestClient

    client = MockQuestClient([
        {"id": "rep-task", "text": "do rep work", "status": "queued", "team_id": "team1"},
    ])
    client.get_ai_profile = profile_client.get_ai_profile
    client.update_ai_profile = profile_client.update_ai_profile
    return client


def test_poller_pulls_rep_skill_before_running_when_resolver_set():
    from quest_ai_runner.config import RunnerConfig
    from quest_ai_runner.runner.poller import Poller

    from .conftest import StubProvider, StubRetrieval

    profile_client = MockProfileClient()
    client = _profile_aware_mock_client(profile_client)
    with tempfile.TemporaryDirectory() as d:
        cfg = RunnerConfig(
            quest_base_url="http://x", quest_api_key="qsk_test", team_id="team1",
            retrieval=StubRetrieval({"README.md": "fact"}),
            model_provider=StubProvider(decisions=[{"action": "answer", "rationale": "ok"}]),
            rep_sync_resolver=lambda task: ("u1", d),
        )
        poller = Poller(cfg, state_path=None, client=client)
        handled = poller.run_once()
        content = Path(d, "SKILL.md").read_text()
    assert handled == ["rep-task"]
    assert profile_client.get_calls == [("team1", "u1")]   # pulled before running
    assert "You are decisive and concise." in content


def test_poller_no_resolver_does_not_sync():
    from quest_ai_runner.config import RunnerConfig
    from quest_ai_runner.runner.poller import Poller

    from .conftest import StubProvider, StubRetrieval

    profile_client = MockProfileClient()
    client = _profile_aware_mock_client(profile_client)
    cfg = RunnerConfig(
        quest_base_url="http://x", quest_api_key="qsk_test", team_id="team1",
        retrieval=StubRetrieval({"README.md": "fact"}),
        model_provider=StubProvider(decisions=[{"action": "answer", "rationale": "ok"}]),
    )
    poller = Poller(cfg, state_path=None, client=client)
    assert poller.run_once() == ["rep-task"]
    assert profile_client.get_calls == []                  # no resolver -> no sync


def test_poller_rep_sync_failure_never_breaks_the_task():
    """A sync that raises (resolver/profile/file error) must not stop the task from running."""
    from quest_ai_runner.config import RunnerConfig
    from quest_ai_runner.runner.poller import Poller

    from .conftest import StubProvider, StubRetrieval

    profile_client = MockProfileClient()
    client = _profile_aware_mock_client(profile_client)
    cfg = RunnerConfig(
        quest_base_url="http://x", quest_api_key="qsk_test", team_id="team1",
        retrieval=StubRetrieval({"README.md": "fact"}),
        model_provider=StubProvider(decisions=[{"action": "answer", "rationale": "ok"}]),
        rep_sync_resolver=lambda task: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    poller = Poller(cfg, state_path=None, client=client)
    assert poller.run_once() == ["rep-task"]                # task still ran
    assert client.reports[0][:2] == ("rep-task", "done")
