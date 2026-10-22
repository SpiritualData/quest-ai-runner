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

def test_push_unchanged_file_pushes_nothing():
    """Right after a pull the file equals its baseline, so push must not PUT anything (a PUT of
    the pulled copy would replace the server's learned_notes and overwrite other writers)."""
    client = MockProfileClient()
    with tempfile.TemporaryDirectory() as d:
        pull_rep_to_skill(client, "team1", "u1", d)
        res = push_skill_to_rep(client, "team1", "u1", d)
    assert client.updates == []
    assert not res.pushed and "nothing to push" in res.notes


def test_push_local_persona_edit_sends_persona_only():
    client = MockProfileClient()
    with tempfile.TemporaryDirectory() as d:
        pull_rep_to_skill(client, "team1", "u1", d)
        p = Path(d, "SKILL.md")
        p.write_text(p.read_text().replace("decisive and concise", "warm and thorough"))
        res = push_skill_to_rep(client, "team1", "u1", d)
        again = push_skill_to_rep(client, "team1", "u1", d)   # baseline advanced: no resend
    assert len(client.updates) == 1
    up = client.updates[0]
    assert up["team_id"] == "team1" and up["user_id"] == "u1"
    assert up["persona"] == "You are warm and thorough."
    assert up["learned_notes"] is None and up["display_name"] is None
    assert res.pushed and res.sent_fields == ["persona"]
    assert not again.pushed


def test_push_merges_local_note_onto_server_note_added_meanwhile():
    """A note added on Quest between pull and push, and one added locally, both survive; the
    untouched server notes go back whole (created_at/source kept, not restamped)."""
    client = MockProfileClient()
    with tempfile.TemporaryDirectory() as d:
        pull_rep_to_skill(client, "team1", "u1", d)
        server_note = {"id": "note_3", "text": "cite sources", "created_at": "2026-10-21",
                       "source": "correction"}
        client._profile["learned_notes"] = client._profile["learned_notes"] + [server_note]
        p = Path(d, "SKILL.md")
        p.write_text(p.read_text().replace(
            "- <!-- id:note_2 --> never schedule meetings on Fridays",
            "- <!-- id:note_2 --> never schedule meetings on Fridays\n- reply within a day"))
        push_skill_to_rep(client, "team1", "u1", d)
    assert len(client.updates) == 1
    up = client.updates[0]
    assert up["persona"] is None                         # persona untouched locally
    notes = up["learned_notes"]
    assert [n.get("id") for n in notes] == ["note_1", "note_2", "note_3", None]
    assert notes[0] == {"id": "note_1", "text": "be concise in status updates",
                        "created_at": "2026-01-01"}      # server dict passes through whole
    assert notes[2] == server_note
    assert notes[3] == {"text": "reply within a day"}


def test_push_local_note_removal_and_edit_apply_to_server_list():
    client = MockProfileClient()
    with tempfile.TemporaryDirectory() as d:
        pull_rep_to_skill(client, "team1", "u1", d)
        p = Path(d, "SKILL.md")
        text = p.read_text()
        text = text.replace("- <!-- id:note_1 --> be concise in status updates\n", "")
        text = text.replace("never schedule meetings on Fridays", "no meetings on Fridays")
        p.write_text(text)
        push_skill_to_rep(client, "team1", "u1", d)
    assert client.updates[0]["learned_notes"] == [{"id": "note_2", "text": "no meetings on Fridays"}]


def test_push_without_baseline_pushes_nothing():
    """A file from before baselines existed must not blindly replace server data."""
    client = MockProfileClient()
    with tempfile.TemporaryDirectory() as d:
        Path(d, "SKILL.md").write_text(render_skill_file("", {
            "persona": "LOCAL", "learned_notes": [{"id": "x", "text": "stale"}]}))
        res = push_skill_to_rep(client, "team1", "u1", d)
    assert client.updates == [] and client.get_calls == []
    assert not res.pushed and "no baseline: push skipped" in res.notes


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
        p = Path(d, "SKILL.md")
        p.write_text(p.read_text().replace("decisive", "gentle"))
        res = sync_rep(client, "team1", "u1", d, direction="push")
    assert res.direction == "push" and res.pushed


def test_sync_rep_both_pulls_then_pushes():
    client = MockProfileClient()
    with tempfile.TemporaryDirectory() as d:
        res = sync_rep(client, "team1", "u1", d, direction="both")
    assert res.pulled and res.direction == "both"
    assert client.get_calls == [("team1", "u1")]
    assert client.updates == [] and not res.pushed   # nothing edited since the pull


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


# --- regression: persona that is a whole SKILL.md must never nest managed blocks -------

PERSONA_BLOCK_START = "<!-- QAR:MANAGED:persona START -->"
PERSONA_BLOCK_END = "<!-- QAR:MANAGED:persona END -->"


def _marker_counts(text):
    return (text.count("QAR:MANAGED:persona START"), text.count("QAR:MANAGED:persona END"),
            text.count("QAR:MANAGED:learned START"), text.count("QAR:MANAGED:learned END"))


def test_pull_with_whole_skill_file_as_persona_does_not_grow_or_nest(tmp_path):
    """A persona pushed as the entire SKILL.md (which holds managed blocks) used to be rendered
    back inside the managed persona block, nesting the file in itself on every pull."""
    skill_dir = tmp_path / "rep"
    skill_dir.mkdir()
    path = skill_dir / "SKILL.md"
    path.write_text(
        "---\nname: rep\n---\n# Rep\nHuman text.\n\n"
        f"{PERSONA_BLOCK_START}\nold persona\n{PERSONA_BLOCK_END}\n\n"
        "<!-- QAR:MANAGED:learned START -->\n## Learned corrections\n\n- a note\n"
        "<!-- QAR:MANAGED:learned END -->\n", encoding="utf-8")
    client = MockProfileClient({"persona": path.read_text(encoding="utf-8"), "learned_notes": []})
    sizes = []
    for _ in range(5):
        pull_rep_to_skill(client, "t1", "u1", str(skill_dir))
        text = path.read_text(encoding="utf-8")
        sizes.append(len(text))
        assert _marker_counts(text) == (1, 1, 1, 1)
        # feed the file back as the persona, exactly like the cockpit provisioning does
        client = MockProfileClient({"persona": text, "learned_notes": []})
    assert sizes[1] == sizes[2] == sizes[3] == sizes[4]


def test_render_strips_markers_from_persona_and_learned_notes():
    nested = f"intro\n{PERSONA_BLOCK_START}\ninner\n{PERSONA_BLOCK_END}\nouter\n{PERSONA_BLOCK_END}"
    out = render_skill_file("", {"persona": nested, "learned_notes": [
        {"id": "n1", "text": f"note {PERSONA_BLOCK_START} x"}]})
    assert _marker_counts(out) == (1, 1, 1, 1)
    assert "intro" in out and "outer" in out
    assert parse_skill_file(out)["persona"].startswith("intro")


def test_replace_between_refuses_body_containing_its_own_markers():
    from quest_ai_runner.runner._managed_sections import ManagedSectionError, replace_between
    with pytest.raises(ManagedSectionError):
        replace_between("", PERSONA_BLOCK_START, PERSONA_BLOCK_END,
                        f"x {PERSONA_BLOCK_END} y")


def test_push_never_sends_a_persona_containing_markers(tmp_path):
    skill_dir = tmp_path / "rep"
    skill_dir.mkdir()
    client = MockProfileClient()
    pull_rep_to_skill(client, "t1", "u1", str(skill_dir))   # records the baseline
    # a corrupt, already-nested file on disk
    (skill_dir / "SKILL.md").write_text(
        f"{PERSONA_BLOCK_START}\nreal\n{PERSONA_BLOCK_START}\nnested\n{PERSONA_BLOCK_END}\n"
        f"{PERSONA_BLOCK_END}\n", encoding="utf-8")
    push_skill_to_rep(client, "t1", "u1", str(skill_dir))
    assert "QAR:MANAGED" not in client.updates[0]["persona"]
