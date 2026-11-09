"""Run ONE queued task the way the lane's production runner would, with every write confined: the lane's own configuration builds the runner, the task
executor runs the task (the brain, then the deep harness the lane selected), and a dry run (``QAR_DRY_RUN_WRITES``) refuses and records every write,
with the real deep worker kept (``QAR_DRY_RUN_DEEP=real``): its own tool processes inherit the dry run, so a Quest write it asks for is refused and
recorded there too (all of them in one shared record file), and it works in a scratch folder (no goal-folder mapping).

Outward acts outside Quest are closed: every request leaves through a local proxy that refuses the hosts that send, post or publish (mail, chat, social,
code hosts, spreadsheets and documents) and lets reading through (web pages, search); git cannot push, the mail key is one that is refused, and the worker
has only the MCP servers the runner passes. Quest itself is reached only through a local read forwarder (GET and HEAD pass, every other method is refused
and recorded) and its real host is refused at the proxy, so no request, by a tool or typed by hand, can change Quest.

Used by the overlay replay (``impls/prod_chat_overlay.py``) after a chat turn queued background work: the turn's recorded task is run here, and what it
made is judged with the turn. A milestone deliverable the worker tried to keep (a refused goal write) counts as the attempted keep: it is measured the
same way the completion step measures a kept one.

    python run_queued_task.py --config LANE_TOML [--env-file LANE_ENV] --task TASK_JSON_FILE

Prints one line ``@@TASK@@ {json}`` with ``{status, result, measured, writes, attempted_keep, refused_hosts, refused_quest_requests, model_calls, error}``.
"""
import argparse
import http.client
import json
import os
import select
import socket
import ssl
import sys
import tempfile
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

#: Hosts (and their subdomains) that send, post or publish: no request reaches them from a replay. Everything else may be read.
OUTWARD_HOSTS = ("resend.com", "sendgrid.net", "sendgrid.com", "mailgun.net", "postmarkapp.com", "smtp.gmail.com", "gmail.googleapis.com",
                 "sheets.googleapis.com", "docs.googleapis.com", "drive.googleapis.com", "chat.googleapis.com", "calendar.googleapis.com",
                 "slides.googleapis.com", "script.googleapis.com", "hooks.slack.com", "slack.com", "discord.com", "discordapp.com", "api.github.com",
                 "github.com", "gitlab.com", "bitbucket.org", "api.twitter.com", "api.x.com", "graph.facebook.com", "api.linkedin.com",
                 "api.notion.com", "api.trello.com", "api.telegram.org", "api.twilio.com", "api.buffer.com", "youtube.googleapis.com",
                 "www.googleapis.com", "oauth2.googleapis.com", "api.tactiq.io")
#: Credentials an outward act would use, taken out of the replay's environment (the worker inherits it).
OUTWARD_UNSET = ("SSH_AUTH_SOCK", "GH_TOKEN", "GITHUB_TOKEN", "GOOGLE_APPLICATION_CREDENTIALS", "SLACK_BOT_TOKEN", "DISCORD_TOKEN", "NOTION_API_KEY")
#: Tools a worker keeps a goal's text with; a refused call of one is the attempted keep.
GOAL_WRITE_TOOLS = ("keep_milestone_deliverable", "create_or_update_quest_goal", "update_goal", "update_quest_goal", "create_goal", "create_quest_goal")
GOAL_TEXT_ARGS = ("body", "description", "text", "notes")


def outward(host: str) -> bool:
    """True when ``host`` is one of ``OUTWARD_HOSTS`` or a subdomain of one."""
    host = host.lower().strip(".")
    return any(host == h or host.endswith("." + h) for h in OUTWARD_HOSTS)


class FilteringProxy:
    """A local HTTPS proxy (CONNECT only) that refuses the outward hosts and tunnels the rest, so reading works and sending does not."""

    def __init__(self, refuse: tuple = ()) -> None:
        self.refuse = tuple(h.lower() for h in refuse)        # more hosts refused here (the real Quest host: Quest is reached only through the read forwarder)
        self.server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server.bind(("127.0.0.1", 0))
        self.server.listen(64)
        self.port = self.server.getsockname()[1]
        self.refused = []
        threading.Thread(target=self.serve, daemon=True).start()

    def serve(self) -> None:
        while True:
            client, _ = self.server.accept()
            threading.Thread(target=self.handle, args=(client,), daemon=True).start()

    def handle(self, client: socket.socket) -> None:
        try:
            head = b""
            while b"\r\n\r\n" not in head and len(head) < 65536:
                chunk = client.recv(4096)
                if not chunk:
                    return
                head += chunk
            line = head.split(b"\r\n", 1)[0].decode("latin-1")
            method, target = (line.split(" ") + ["", ""])[:2]
            host, _, port = target.rpartition(":") if method == "CONNECT" else ("", "", "")
            if method != "CONNECT" or not host or outward(host) or host.lower() in self.refuse:
                self.refused.append(f"{method} {target}")
                client.sendall(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
                return
            upstream = socket.create_connection((host, int(port or 443)), timeout=30)
            client.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
            pair = [client, upstream]
            while True:
                ready, _, _ = select.select(pair, [], [], 300)
                if not ready:
                    return
                for sock in ready:
                    data = sock.recv(65536)
                    if not data:
                        return
                    (upstream if sock is client else client).sendall(data)
        except OSError:
            return
        finally:
            client.close()


class QuestReadForwarder:
    """The only way the replay reaches Quest: a local HTTP server that passes GET and HEAD to the real Quest API (directly, over TLS) and refuses and
    records every other method, so no request of the runner or the worker, by tool or by hand, can change Quest."""

    def __init__(self, base_url: str) -> None:
        parts = urlsplit(base_url)
        self.host, self.port, self.prefix = parts.hostname or "", parts.port or (443 if parts.scheme == "https" else 80), parts.path.rstrip("/")
        self.secure = parts.scheme == "https"
        self.refused = []
        forwarder = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args) -> None:      # quiet: the refusals are recorded, the reads are the run's own
                return

            def relay(self) -> None:
                connection = (http.client.HTTPSConnection(forwarder.host, forwarder.port, context=ssl.create_default_context(), timeout=60)
                              if forwarder.secure else http.client.HTTPConnection(forwarder.host, forwarder.port, timeout=60))
                headers = {k: v for k, v in self.headers.items() if k.lower() not in ("host", "connection", "proxy-connection")}
                connection.request(self.command, forwarder.prefix + self.path, headers=headers)
                answer = connection.getresponse()
                body = answer.read()
                self.send_response(answer.status)
                for key, value in answer.getheaders():
                    if key.lower() not in ("transfer-encoding", "connection", "content-length"):
                        self.send_header(key, value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)

            def refuse(self) -> None:
                forwarder.refused.append(f"{self.command} {self.path}")
                body = json.dumps({"detail": "Refused in replay: only reads reach Quest, and nothing was changed."}).encode()
                self.send_response(403)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            do_GET = do_HEAD = relay
            do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = refuse

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()


def no_outward_acts(scratch: str, proxy_url: str) -> dict:
    """The environment that keeps the replayed task from acting outside Quest: every request through the filtering proxy (the model providers and this
    machine directly), git unable to push (no SSH, no credential helper, no gh login), the mail key replaced by one that is refused, and the worker limited
    to the MCP servers the runner passes (none of the person's own connectors)."""
    direct = "localhost,127.0.0.1,::1"
    return {"HTTPS_PROXY": proxy_url, "HTTP_PROXY": proxy_url, "https_proxy": proxy_url, "http_proxy": proxy_url, "ALL_PROXY": proxy_url,
            "NO_PROXY": direct, "no_proxy": direct, "GIT_SSH_COMMAND": "false", "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "false",
            "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "credential.helper", "GIT_CONFIG_VALUE_0": "", "GH_CONFIG_DIR": os.path.join(scratch, "gh"),
            "RESEND_API_KEY": "blocked-in-replay", "QAR_DEEP_STRICT_MCP": "1"}


def attempted_keep(rows, title: str) -> str:
    """The text of the last refused goal write that names the milestone's keyed goal (or of the last one when none names a title), "" when none."""
    for row in reversed(rows or []):
        args = row.get("args") or {}
        if row.get("tool") not in GOAL_WRITE_TOOLS:
            continue
        named = str(args.get("title") or args.get("name") or "").strip()
        if named and title and named != title.strip():
            continue
        text = next((str(args[k]) for k in GOAL_TEXT_ARGS if str(args.get(k) or "").strip()), "")
        if text:
            return text
    return ""


def skill_reads(scratch: str, skill_marker: str = "SKILL.md"):
    """The worker's own tool calls that read a skill file, from its session transcripts (the worker ran in ``scratch``, so its sessions sit in the
    project folder named after that path): one trace line each, never assumed."""
    root = os.path.join(os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude"), "projects")
    slug = scratch.replace("/", "-").replace(".", "-").replace("_", "-")
    found = []
    try:
        folders = [os.path.join(root, d) for d in os.listdir(root) if d.endswith(slug.lstrip("-")) or d == slug]
    except OSError:
        return found
    for folder in folders:
        for name in sorted(os.listdir(folder)):
            if not name.endswith(".jsonl"):
                continue
            with open(os.path.join(folder, name), encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    if skill_marker in line and '"tool_use"' in line:
                        try:
                            event = json.loads(line)
                        except ValueError:
                            continue
                        for block in ((event.get("message") or {}).get("content") or []):
                            if isinstance(block, dict) and block.get("type") == "tool_use" and skill_marker in json.dumps(block.get("input")):
                                found.append(f"{event.get('timestamp', '')} {block.get('name')} {json.dumps(block.get('input'))[:200]}")
    return found


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", required=True, help="the lane's configuration file (the production runner's)")
    parser.add_argument("--task", required=True, help="a JSON file holding the queued task document")
    parser.add_argument("--env-file", default="", help="the lane's KEY=VALUE file, loaded the way the lane loads it")
    args = parser.parse_args(argv)
    scratch = tempfile.mkdtemp(prefix="qar-task-replay-")
    record_file = os.path.join(scratch, "refused_writes.jsonl")
    out = {"status": None, "result": "", "measured": None, "writes": [], "attempted_keep": "", "refused_hosts": [], "refused_quest_requests": [],
           "model_calls": 0, "error": None}
    proxy = forwarder = None
    try:
        with open(args.task, encoding="utf-8") as handle:
            task = json.load(handle)
        if args.env_file:
            from quest_ai_runner.the_consciousness.request_intake.lane import load_env_file
            load_env_file(args.env_file)
        # Quest is reached only through the read forwarder: the runner and the worker get its address, and the real host is refused at the proxy.
        real_quest = (os.environ.get("QUEST_BASE_URL") or "").strip()
        forwarder = QuestReadForwarder(real_quest) if real_quest else None
        proxy = FilteringProxy(refuse=(forwarder.host,) if forwarder else ())
        if forwarder:
            os.environ["QUEST_BASE_URL"] = forwarder.url
        # After the lane's own file, so the confinement always wins over anything it sets.
        os.environ.update({"QAR_DRY_RUN_WRITES": "1", "QAR_DRY_RUN_DEEP": "real", "QAR_DRY_RUN_SCRATCH": scratch, "QAR_DEEP_WORKING_DIR": scratch,
                           "QAR_STATE_PATH": os.path.join(scratch, "state.json"), "QAR_DRY_RUN_RECORD_FILE": record_file})
        os.environ.update(no_outward_acts(scratch, f"http://127.0.0.1:{proxy.port}"))
        for name in OUTWARD_UNSET:
            os.environ.pop(name, None)
        os.chdir(scratch)
        from quest_ai_runner import load_config
        from quest_ai_runner.the_consciousness.request_intake.poller import Poller
        from quest_ai_runner.the_intelligence.critical_thinking.esoi_application import milestone_completion
        from quest_ai_runner.the_world.tools import dry_run_writes
        dry_run_writes.install()
        cfg = load_config(args.config)
        cfg.quest_folder_map = None          # the worker works in the scratch folder, never in a goal's mapped folder
        poller = Poller(cfg, state_path=os.environ["QAR_STATE_PATH"])
        executor = poller.task_executor()
        outcome = executor.execute(task)
        snap = dry_run_writes.RECORDER.snapshot()
        measured = getattr(executor, "milestone_measured", None)
        task_text = str(task.get("text") or task.get("title") or task.get("description") or "")
        record = milestone_completion.work_record_of(task_text)
        shared = dry_run_writes.shared_writes(record_file)
        tried = attempted_keep(shared, (record or {}).get("title", ""))
        out["attempted_keep"] = tried
        if record and tried and (not measured or measured.get("status") == "no_deliverable" or not float(measured.get("after") or 0) > float(record.get("before") or 0)):
            # The worker's refused keep is the attempted keep: measured the same way as a kept deliverable (the scorer, the goal, the metric again).
            again = milestone_completion.measure_on_completion(record, milestone_completion.DELIVERABLE_LINE + "\n" + tried, executor._orch.tools,
                                                               executor._orch.cheap_decision, reference=task_text + "\n\n" + str(outcome.result or ""))
            if not measured or float(again.get("after") or 0) >= float(measured.get("after") or 0):
                measured = dict(again, from_attempted_keep=True)
        out.update(status=outcome.status, result=outcome.result, measured=measured, model_calls=snap["model_calls"],
                   writes=[{k: w.get(k) for k in ("kind", "what", "tool")} for w in snap["writes"] if not w.get("internal")]
                   + [{"kind": "tool (worker)", "what": f"{r.get('tool')} {str(r.get('args'))[:240]}", "tool": r.get("tool")} for r in shared])
    except Exception as error:  # noqa: BLE001  the replay reports what failed; the judge sees it
        out["error"] = f"{type(error).__name__}: {error}"
        traceback.print_exc(file=sys.stderr)
    out["skill_reads"] = skill_reads(scratch)          # the trace that the worker actually read the skill it was told to carry out
    out["refused_hosts"] = list(proxy.refused) if proxy else []
    out["refused_quest_requests"] = list(forwarder.refused) if forwarder else []
    print("@@TASK@@ " + json.dumps(out, default=str, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
