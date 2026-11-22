"""The live deep-run monitor must find the worker's session file under any Claude config base."""
from pathlib import Path

from hubofhubs.api import session as hub_session
from quest_ai_runner.intelligence.mind.delegation import goal_runner


def _session(base: Path, sid: str) -> Path:
    d = base / "projects" / "-some-project"
    d.mkdir(parents=True)
    f = d / f"{sid}.jsonl"
    f.write_text("{}\n")
    return f


def test_session_in_config_dir_found_even_when_working_dir_has_other_sessions(tmp_path, monkeypatch):
    work = tmp_path / "work"
    config = tmp_path / "config"
    home = tmp_path / "home"
    home.mkdir()
    _session(work / ".claude", "other-session")  # working dir holds only an unrelated session
    mine = _session(config, "my-session")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    monkeypatch.setenv("HOME", str(home))
    assert goal_runner._find_session_file("my-session", str(work)) == mine
    assert goal_runner.resolve_session_file(str(work), "my-session") == mine


def test_session_in_home_found_when_working_dir_base_exists(tmp_path, monkeypatch):
    work = tmp_path / "work"
    home = tmp_path / "home"
    _session(work / ".claude", "other-session")
    mine = _session(home / ".claude", "my-session")
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setenv("HOME", str(home))
    assert goal_runner._find_session_file("my-session", str(work)) == mine


def test_missing_session_is_none(tmp_path, monkeypatch):
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert goal_runner._find_session_file("nope", str(tmp_path)) is None


def test_finding_a_session_never_walks_every_recorded_session(tmp_path, monkeypatch):
    """35,000 session files made each lookup a multi-second walk, asked every tenth of a second."""
    home = tmp_path / "home"
    mine = _session(home / ".claude", "my-session")
    for i in range(3):
        (mine.parent.parent / f"-other-{i}").mkdir()
        (mine.parent.parent / f"-other-{i}" / f"s{i}.jsonl").write_text("{}\n")
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setenv("HOME", str(home))

    def refuse(*args, **kwargs):
        raise AssertionError("a recursive search walked the session tree")

    monkeypatch.setattr(Path, "rglob", refuse)
    assert goal_runner._find_session_file("my-session", str(tmp_path / "work")) == mine
    assert goal_runner._find_session_file("nope", str(tmp_path / "work")) is None


def test_session_file_size_is_what_a_resumed_worker_already_wrote(tmp_path, monkeypatch):
    from quest_ai_runner.intelligence.mind.delegation.goal_runner_session import session_file_size

    home = tmp_path / "home"
    mine = _session(home / ".claude", "my-session")
    mine.write_text("x" * 40)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setenv("HOME", str(home))
    assert session_file_size(str(tmp_path / "work"), "my-session") == 40
    assert session_file_size(str(tmp_path / "work"), "never-written") == 0
    assert session_file_size(str(tmp_path / "work"), None) == 0


def test_a_resumed_session_does_not_replay_what_the_person_already_saw(tmp_path, monkeypatch):
    import json
    import threading
    import time

    from quest_ai_runner.intelligence.mind.delegation.goal_runner_session import _monitor_claude_session, session_file_size

    project = tmp_path / ".claude" / "projects" / "proj"
    project.mkdir(parents=True)
    monkeypatch.setattr(hub_session, "find_claude_project_dir", lambda working_dir, config_dir=None: project)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "nohome"))
    session = project / "11111111-1111-1111-1111-111111111111.jsonl"

    def write(text):
        with open(session, "a") as f:
            f.write(json.dumps({"type": "assistant", "message": {"content": text}}) + "\n")

    write("first spawn: read the file")
    write("first spawn: edited the file")
    already = session_file_size(str(tmp_path), session.stem)
    assert already == session.stat().st_size > 0

    def watch(start_offset):
        events = []
        stop = threading.Event()
        thread = threading.Thread(
            target=_monitor_claude_session, args=(str(tmp_path), events.append, stop),
            kwargs={"poll_interval": 0.02, "session_id": session.stem, "forced_run_id": "run1",
                    "heartbeat_interval": 100.0, "start_offset": start_offset}, daemon=True)
        thread.start()
        time.sleep(0.25)
        stop.set()
        thread.join(timeout=2)
        return [e.text for e in events if "spawn" in (e.text or "") or "resumed" in (e.text or "")]

    assert len(watch(0)) == 2, "a first spawn reads its file from the start"
    write("resumed worker: recorded the metric")
    seen = watch(already)
    assert len(seen) == 1 and "resumed worker" in seen[0]


def test_recency_floor_counts_only_conversations_in_scope(tmp_path):
    """Another person's recent conversations must not take the recency floor slots of this person."""
    import json
    from quest_ai_runner.intelligence.higher_mind.long_term_memory.session_file_conversation_store import SessionFileConversationStore
    convs = [("a1", "alice", 100), ("a2", "alice", 200), ("a3", "alice", 300),
             ("b1", "bob", 400), ("b2", "bob", 500), ("b3", "bob", 600)]
    for cid, user, ts in convs:
        (tmp_path / f"{cid}.json").write_text(json.dumps({
            "id": cid, "user_id": user, "updated_at": ts,
            "messages": [{"role": "user", "text": f"topic {cid} zzz"}]}), encoding="utf-8")
    store = SessionFileConversationStore(sessions_dir=str(tmp_path))
    ctx = store.related_slices("nothing matches this query", {"user_id": "alice"}, max_convs=3)
    ids = {str(s.get("conv_id", "")) for s in ctx.sources}
    assert any(i.endswith("a3.json") or i.endswith("a3") for i in ids), ids
    assert not any("b" in i.split("/")[-1][:1] for i in ids), ids
