"""The built-in lane supervisor, against a harmless fake long-running command (never a real lane)."""
import os
import subprocess
import threading
import time
from pathlib import Path

import pytest

from quest_ai_runner.the_consciousness.request_intake import lane_supervisor as sup
from quest_ai_runner.the_body.configuration.lane_env_file import parse_env_text


@pytest.fixture
def spec(tmp_path):
    return sup.LaneSpec("fake", tmp_path / "state", ["sleep", "300"], restart_delay=0.2)


@pytest.fixture(autouse=True)
def cleanup(spec):
    yield
    sup.stop_lane(spec, grace=2, say=lambda message: None)


def quiet(message):
    pass


def test_ensure_revives_dead_lane_and_leaves_live_one(spec):
    assert sup.ensure_lane(spec, quiet)
    running, first = sup.lane_status(spec)
    assert running
    assert sup.ensure_lane(spec, quiet)
    assert sup.lane_status(spec) == (True, first)
    os.killpg(first, 9)
    deadline = time.monotonic() + 5
    while sup.lane_status(spec)[0] and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not sup.lane_status(spec)[0]
    assert sup.ensure_lane(spec, quiet)
    running, second = sup.lane_status(spec)
    assert running and second != first


def test_concurrent_ensure_starts_one(spec):
    results = []
    threads = [threading.Thread(target=lambda: results.append(sup.ensure_lane(spec, quiet))) for i in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert results == [True] * 4
    running, pid = sup.lane_status(spec)
    assert running
    supervisors = [p for p, comm in sup.process_group_members(pid) if comm.startswith("python")]
    assert len(supervisors) == 1
    assert spec.log_path.read_text().count("supervisor up") == 1


def test_lane_restarts_after_child_exit(tmp_path):
    marker = tmp_path / "runs"
    quick = sup.LaneSpec("quick", tmp_path / "s2", ["sh", "-c", f"echo x >> {marker}"], restart_delay=0.2)
    try:
        sup.start_lane(quick, quiet)
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline and (not marker.exists() or len(marker.read_text().split()) < 3):
            time.sleep(0.1)
        assert len(marker.read_text().split()) >= 3
    finally:
        sup.stop_lane(quick, grace=2, say=quiet)


def test_stop_kills_group_including_children(spec):
    spec.command = ["sh", "-c", "sleep 300 & sleep 300 & wait"]
    sup.start_lane(spec, quiet)
    _, pid = sup.lane_status(spec)
    deadline = time.monotonic() + 5
    while len(sup.process_group_members(pid)) < 4 and time.monotonic() < deadline:
        time.sleep(0.05)
    assert len(sup.process_group_members(pid)) >= 4
    assert sup.stop_lane(spec, grace=5, say=quiet)
    assert sup.process_group_members(pid) == []
    assert not sup.lane_status(spec)[0]


def make_fake_claude(tmp_path):
    fake = tmp_path / "bin" / "claude"
    fake.parent.mkdir()
    fake.write_text("#!/bin/sh\nsleep 300\n")  # a script named claude: its comm is "claude"
    fake.chmod(0o755)
    return fake


def test_restart_if_idle_skips_while_deep_child_present(spec, tmp_path):
    fake = make_fake_claude(tmp_path)
    spec.command = ["sh", "-c", f"{fake} & wait"]
    sup.start_lane(spec, quiet)
    _, pid = sup.lane_status(spec)
    deadline = time.monotonic() + 5
    while not sup.deep_run_in_flight(pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    messages = []
    assert sup.restart_lane(spec, if_idle=True, grace=2, say=messages.append)
    assert any("deep run in flight" in m for m in messages)
    assert sup.lane_status(spec) == (True, pid)


def test_restart_if_idle_restarts_without_deep_child(spec):
    sup.start_lane(spec, quiet)
    _, pid = sup.lane_status(spec)
    messages = []
    assert sup.restart_lane(spec, if_idle=True, grace=2, say=messages.append)
    running, new_pid = sup.lane_status(spec)
    assert running and new_pid != pid
    assert not any("skipping" in m for m in messages)


def test_deep_run_check_is_injectable():
    assert sup.deep_run_in_flight(1, members=lambda group: [(5, "python"), (6, "node")]) == (6, "node")
    assert sup.deep_run_in_flight(1, members=lambda group: [(5, "python")]) is None
    assert sup.deep_run_in_flight(1, comms=("agent",), members=lambda group: [(7, "agent")]) == (7, "agent")


def test_env_file_reaches_lane_and_is_reread(tmp_path):
    envfile = tmp_path / "lane.env"
    envfile.write_text("export GREETING='hello world'\n")
    out = tmp_path / "out"
    spec = sup.LaneSpec("envlane", tmp_path / "s3", ["sh", "-c", f'echo "$GREETING" >> {out}'],
                        env_file=envfile, restart_delay=0.2)
    try:
        sup.start_lane(spec, quiet)
        deadline = time.monotonic() + 5
        while (not out.exists() or "hello world" not in out.read_text()) and time.monotonic() < deadline:
            time.sleep(0.1)
        envfile.write_text("GREETING=second\n")
        while "second" not in out.read_text() and time.monotonic() < deadline + 5:
            time.sleep(0.1)
        assert "hello world" in out.read_text() and "second" in out.read_text()
    finally:
        sup.stop_lane(spec, grace=2, say=quiet)


def test_env_file_parsing():
    text = "\n".join([
        "# a comment", "", "; another", "PLAIN=value", "export EXPORTED=yes", "QUOTED=\"two words\"",
        "SINGLE='keep $HOME and `date`'", "DOUBLE=\"line\\nbreak and \\\"q\\\"\"", "EMPTY=",
        "TRAILING=abc # note", "HASH=a#b", "SHELL=$(echo hi)$HOME", "bad line", "1BAD=x", "  SPACED = padded  ",
    ])
    values = parse_env_text(text)
    assert values["PLAIN"] == "value"
    assert values["EXPORTED"] == "yes"
    assert values["QUOTED"] == "two words"
    assert values["SINGLE"] == "keep $HOME and `date`"
    assert values["DOUBLE"] == 'line\nbreak and "q"'
    assert values["EMPTY"] == ""
    assert values["TRAILING"] == "abc"
    assert values["HASH"] == "a#b"
    assert values["SHELL"] == "$(echo hi)$HOME"
    assert values["SPACED"] == "padded"
    assert "bad line" not in values and "1BAD" not in values


def test_default_names_and_state_dir(tmp_path):
    assert sup.default_lane_name("/x/team-a/qar.toml") == "team-a-qar"
    assert sup.default_lane_name(None) == "default"
    assert sup.default_state_dir("n", {"XDG_STATE_HOME": str(tmp_path)}) == tmp_path / "quest-ai-runner" / "lanes" / "n"
