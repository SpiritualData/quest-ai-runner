"""The overlay arm runs the background work a turn queued, the way the lane's runner would, and judges its kept deliverable with the turn. SYNTHETIC
records only.
Run:  python -m pytest evaluation/components/chat_quality/test_overlay_queued_task.py -q"""
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "impls"))

import prod_chat_overlay  # noqa: E402
import value_gate  # noqa: E402

TASK = {"id": "t1", "text": "Make the scripts.", "quest_id": "q1"}
RUN = {"status": "done", "result": "Wrote the script from the product guide.",
       "measured": {"title": "M1 Tutorials deliverables", "goal_found": True, "body": "Overview:\nScripts.\n\n--- Part 1 ---\nOpening line.",
                    "line": "Scripts meeting the rubric: 0 before, 1 after of 6 planned", "milestone_metric": {"id": "met-1", "record": "Current value: 0 of 6"},
                    "increment": {"kind": "deliverable", "measure_id": "met-x", "before": 0, "after": 1, "total": 6, "method": "rubric", "changed": True}}}


def test_a_queued_task_is_found_whole_in_the_turns_writes():
    writes = [{"collection": "goals", "method": "update_one"}, {"collection": "assistant_tasks", "method": "insert_one", "document": TASK}]
    assert prod_chat_overlay.queued_tasks(writes) == [TASK]


def test_without_the_lane_configuration_the_task_is_not_run_and_says_why():
    assert "not set" in prod_chat_overlay.run_queued(TASK, environ={})["error"]


def test_the_tasks_measured_keep_is_a_read_the_value_gate_counts_and_the_judge_is_told_it():
    read = prod_chat_overlay.task_read(RUN)
    case = {"ai_doable": [{"kind": "deliverable"}], "increment_measure": {"lands_on": {"title_prefix": "M1 "}}}
    gate = value_gate.evaluate(case, [read], [], {"failure_class": None, "rubric": []})
    assert gate["D"] == 1 and gate["L"] == 1 and gate["M"] == 1
    notes = prod_chat_overlay.task_notes([RUN])
    assert "QUEUED TASK 1" in notes and "Opening line." in notes and "0 before, 1 after of 6 planned" in notes
    assert prod_chat_overlay.task_read({"error": "x"}) is None


def test_the_replayed_task_reads_the_web_but_cannot_send_post_or_push():
    import socket
    import run_queued_task as rq
    assert rq.outward("api.resend.com") and rq.outward("sheets.googleapis.com") and rq.outward("discord.com") and rq.outward("github.com")
    assert not rq.outward("example.org") and not rq.outward("api.anthropic.com") and not rq.outward("generativelanguage.googleapis.com")
    proxy = rq.FilteringProxy()
    with socket.create_connection(("127.0.0.1", proxy.port), timeout=5) as conn:
        conn.sendall(b"CONNECT api.resend.com:443 HTTP/1.1\r\nHost: api.resend.com:443\r\n\r\n")
        assert conn.recv(100).startswith(b"HTTP/1.1 403")
    with socket.create_connection(("127.0.0.1", proxy.port), timeout=5) as conn:
        conn.sendall(b"POST http://hooks.example/x HTTP/1.1\r\nHost: hooks.example\r\n\r\n")          # plain HTTP never passes
        assert conn.recv(100).startswith(b"HTTP/1.1 403")
    env = rq.no_outward_acts("/tmp/x", f"http://127.0.0.1:{proxy.port}")
    assert env["GIT_SSH_COMMAND"] == "false" and env["GIT_CONFIG_VALUE_0"] == "" and env["RESEND_API_KEY"] == "blocked-in-replay" and env["QAR_DEEP_STRICT_MCP"] == "1"


def test_the_workers_refused_goal_write_is_the_attempted_keep():
    import run_queued_task as rq
    rows = [{"tool": "create_or_update_quest_goal", "args": {"title": "Other goal", "description": "not this"}},
            {"tool": "list_quest_goals", "args": {}},
            {"tool": "create_or_update_quest_goal", "args": {"title": "M1 deliverables", "description": "Kept by Quest AI: the scripts"}}]
    assert rq.attempted_keep(rows, "M1 deliverables") == "Kept by Quest AI: the scripts" and rq.attempted_keep(rows[:2], "M1 deliverables") == ""


def test_quest_is_reached_only_through_the_read_forwarder():
    import subprocess
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import run_queued_task as rq
    seen = []

    class Upstream(BaseHTTPRequestHandler):
        def log_message(self, *args):
            return

        def do_GET(self):
            seen.append(("GET", self.path, self.headers.get("Authorization")))
            body = b'{"ok": true}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            seen.append(("POST", self.path, None))
            self.send_response(201)
            self.end_headers()
    upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    threading.Thread(target=upstream.serve_forever, daemon=True).start()
    forwarder = rq.QuestReadForwarder(f"http://127.0.0.1:{upstream.server_address[1]}")

    def curl(*extra):
        return subprocess.run(["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", "--noproxy", "*", *extra], capture_output=True, text=True, timeout=10).stdout
    assert curl("-H", "Authorization: Bearer k", forwarder.url + "/api/goals?x=1") == "200"
    for method in ("POST", "PATCH", "DELETE", "PUT"):
        assert curl("-X", method, "-d", "{}", forwarder.url + "/api/goals/g1") == "403"
    assert seen == [("GET", "/api/goals?x=1", "Bearer k")]                     # nothing but the read reached Quest
    assert forwarder.refused == ["POST /api/goals/g1", "PATCH /api/goals/g1", "DELETE /api/goals/g1", "PUT /api/goals/g1"]
    proxy = rq.FilteringProxy(refuse=("quest.example",))
    import socket
    with socket.create_connection(("127.0.0.1", proxy.port), timeout=5) as conn:
        conn.sendall(b"CONNECT quest.example:443 HTTP/1.1\r\nHost: quest.example:443\r\n\r\n")
        assert conn.recv(100).startswith(b"HTTP/1.1 403")


def test_the_workers_skill_read_is_traced_from_its_own_session(tmp_path, monkeypatch):
    import json as js
    import run_queued_task as rq
    scratch = "/tmp/qar-task-replay-abc"
    folder = tmp_path / "projects" / scratch.replace("/", "-")
    folder.mkdir(parents=True)
    event = {"timestamp": "t1", "message": {"content": [{"type": "tool_use", "name": "Read", "input": {"file_path": "/skills/method-x/SKILL.md"}}]}}
    (folder / "s.jsonl").write_text(js.dumps(event) + "\n" + js.dumps({"message": {"content": [{"type": "text", "text": "SKILL.md"}]}}) + "\n")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    lines = rq.skill_reads(scratch)
    assert len(lines) == 1 and "Read" in lines[0] and "method-x/SKILL.md" in lines[0]
