"""One lane, several teams: ``RunnerConfig.team_ids`` (QUEST_TEAM_IDS).

A runner used to be able to discover work for exactly ONE team, so an org running four teams ran
four processes that differed only in which single team each polled -- identical accounts,
identical corpora, identical everything else. ``team_ids`` removes that duplication: the lane
sends the whole team set in ONE request and serves all of them.

The rule these tests exist to hold is that the addition is INVISIBLE to every existing
deployment. ``team_ids`` empty (the default) must produce byte-identical calls to what the runner
sent before the field existed, which is why several of the assertions below are about the exact
string in the team query param rather than about behaviour. The two older backward-compatibility
tests in ``test_runner.py``
(``test_poller_discovery_is_team_scoped_to_the_lanes_team`` and
``test_poller_teamless_lane_discovers_owner_scoped``) are the other half of that proof and are
deliberately left untouched.
"""
from __future__ import annotations

import threading

from quest_ai_runner.config import RunnerConfig
from quest_ai_runner.runner.poller import Poller
from quest_ai_runner.runner.quest_client import QuestClient

from .conftest import StubProvider, StubRetrieval
from .test_runner import MockQuestClient


def _lane(client, provider=None, *, team_id="team1", **cfg_overrides):
    cfg = RunnerConfig(
        quest_base_url="http://x", quest_api_key="qsk_test", team_id=team_id,
        retrieval=StubRetrieval({"README.md": "fact"}),
        model_provider=provider or StubProvider(
            decisions=[{"action": "answer", "rationale": "ok"}] * 5),
        **cfg_overrides,
    )
    return Poller(cfg, state_path=None, client=client)


# --- the client's team param: the byte-identical guarantee ---------------------------------

def test_one_team_in_team_ids_is_the_same_string_as_that_team_alone():
    """THE core guarantee. ``",".join(["t"]) == "t"``, so a one-team set and a bare team id are
    indistinguishable on the wire -- which is what makes it safe to route every discovery call
    through the new helper, including on lanes that never set ``team_ids``."""
    client = QuestClient("http://x", "qsk_test", team_id="team1")
    assert client.team_param("team1", None) == client.team_param(None, ["team1"]) == "team1"


def test_no_team_ids_falls_back_to_exactly_the_old_behaviour():
    client = QuestClient("http://x", "qsk_test", team_id="team1")
    assert client.team_param(None, None) == "team1"     # the client's configured team
    assert client.team_param(None, []) == "team1"       # an empty set is not a set
    assert client.team_param("team9", None) == "team9"  # an explicit team wins
    assert client.team_param("", None) == ""            # "" is owner-scoped, not "unset"


def test_several_teams_become_one_comma_joined_value_in_order():
    client = QuestClient("http://x", "qsk_test", team_id="team1")
    assert client.team_param(None, ["team1", "team2", "team3"]) == "team1,team2,team3"


# --- discovery over the set ----------------------------------------------------------------

def test_lane_discovers_every_configured_team_and_no_others():
    client = MockQuestClient([
        {"id": "t1-task", "text": "a", "status": "queued", "team_id": "team1"},
        {"id": "t2-task", "text": "b", "status": "queued", "team_id": "team2"},
        {"id": "t3-task", "text": "c", "status": "queued", "team_id": "team3"},
        {"id": "other-task", "text": "d", "status": "queued", "team_id": "team_other"},
    ])
    poller = _lane(client, team_ids=["team1", "team2", "team3"])
    handled = poller.run_once()
    assert sorted(handled) == ["t1-task", "t2-task", "t3-task"]
    assert "other-task" not in client.claimed
    # ONE request carrying the whole set, not one request per team.
    assert client.discover_team_ids == ["team1,team2,team3"]


def test_empty_team_ids_sends_exactly_what_it_always_sent():
    """The no-op proof for every existing single-team deployment: the recorded param is the bare
    team id, not a one-element list, not a trailing comma, not None."""
    client = MockQuestClient([{"id": "t1", "text": "a", "status": "queued", "team_id": "team1"}])
    poller = _lane(client)
    assert poller.cfg.team_ids == []          # the default nobody has to set
    poller.run_once()
    assert client.discover_team_ids == ["team1"]
    assert poller.discovery_team_ids() == ["team1"]


def test_discovery_team_id_wins_over_team_ids():
    """Documented precedence: the older, narrower knob wins when it is set AT ALL -- including
    the explicit "" that means owner-scoped. The two settings are a contradiction, and honouring
    the older one is what guarantees no existing lane changes behaviour by gaining this field."""
    client = MockQuestClient([])
    poller = _lane(client, team_ids=["team2", "team3"], discovery_team_id="")
    assert poller.discovery_team_ids() == [""]
    poller.run_once()
    assert client.discover_team_ids == [""]

    poller = _lane(client, team_ids=["team2", "team3"], discovery_team_id="team9")
    assert poller.discovery_team_ids() == ["team9"]


# --- the fast lane: one long-poll for the whole set ------------------------------------------

def test_fast_lane_sends_the_whole_set_in_one_wait_call():
    """``wait_for_interactive`` returns ONE FIFO-oldest task for the scope it was given. A lane
    that waited per team and filtered client-side would discard another team's task on every
    reconnect and spin. So: exactly one call, carrying every team."""
    client = MockQuestClient([
        {"id": "rt", "text": "live", "status": "queued", "team_id": "team2", "real_time": True},
    ])
    poller = _lane(client, team_ids=["team1", "team2", "team3"])
    stop = threading.Event()

    dispatched = []
    poller._dispatch_fast_task = dispatched.append  # keep the thread pool out of this test
    original_wait = client.wait_for_interactive

    def wait_once(**kwargs):
        task = original_wait(**kwargs)
        stop.set()      # one iteration is all this test needs
        return task

    client.wait_for_interactive = wait_once
    poller._fast_lane_loop(stop)

    assert client.wait_calls == ["team1,team2,team3"]   # ONE call, the whole set
    assert [t["id"] for t in dispatched] == ["rt"]


def test_fast_lane_runs_for_a_lane_that_has_only_team_ids():
    """The old guard demanded a truthy ``cfg.team_id``. A lane configured purely with a team SET
    would have had its fast lane return immediately and silently, leaving live chat work to the
    15-minute background scan."""
    client = MockQuestClient([])
    poller = _lane(client, team_id="", team_ids=["team2", "team3"])
    stop = threading.Event()
    calls = []

    def wait_once(**kwargs):
        calls.append(kwargs)
        stop.set()
        return None

    client.wait_for_interactive = wait_once
    poller._fast_lane_loop(stop)
    assert calls, "the fast lane returned without ever polling"
    assert calls[0]["team_ids"] == ["team2", "team3"]


# --- heartbeat ------------------------------------------------------------------------------

def test_heartbeat_fires_once_per_team_in_the_union():
    """The heartbeat is how a team learns this environment exists and what it can do. A team
    whose work the lane runs but which never hears from it shows the lane as absent."""
    client = MockQuestClient([])
    poller = _lane(client, team_ids=["team2", "team1", "team3"])
    poller.run_once()
    # Home team first, then the set, deduplicated (team1 appears in both).
    assert [hb[0] for hb in client.heartbeats] == ["team1", "team2", "team3"]


def test_single_team_lane_still_heartbeats_exactly_once():
    client = MockQuestClient([])
    poller = _lane(client)
    poller.run_once()
    assert [hb[0] for hb in client.heartbeats] == ["team1"]


# --- per-task team resolution ----------------------------------------------------------------

def test_rep_sync_uses_the_tasks_own_team_not_the_lanes_home_team(monkeypatch):
    """Per-task work follows the TASK's team. This was already true in the code (a task carries
    its own ``team_id``); the test pins it, because it is the property that makes serving several
    teams from one lane correct at all rather than merely possible."""
    from quest_ai_runner.runner import rep_sync

    pushed = []
    monkeypatch.setattr(rep_sync, "push_skill_to_rep",
                        lambda client, team_id, user_id, skill_dir: pushed.append(team_id))

    poller = _lane(MockQuestClient([]), rep_sync_direction="push")
    poller._push_rep_for({"id": "x", "team_id": "team2"}, ("user_7", "/tmp/skill"))
    poller._push_rep_for({"id": "y"}, ("user_7", "/tmp/skill"))   # no team on the task

    assert pushed == ["team2", "team1"]   # the task's own team; the lane's team only as fallback
