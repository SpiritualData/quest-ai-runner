"""REAL turns for the interim-reply and ask metric: the real orchestrator and the real goal loop on the Claude Code subscription (haiku),
real web search for the deep worker, a seeded fictional card store, and an in-memory decision sink that behaves like the backend's
(one open ask per ``dedupe_key``, an assignee who is not a member of the team is refused). Nothing here calls the paid API:
``QAR_MODEL_BACKEND=claude_cli`` and no ``ANTHROPIC_API_KEY`` survives the allowlist.

    python3 evaluation/components/interim_reply/live_turns.py --turn research --arm new --out /tmp/research_new.json

``--arm new`` runs with ``OrchestratorConfig.interim_replies=True``; ``--arm today`` runs with it False (the incumbent). The same wiring
otherwise, so the arms differ in the switch only. Each run records the events, the stored-message list a consumer would write, every
verdict the goal loop read, every decision of the interim policy and every ask filed; ``check`` then derives the truthfulness problems
from that record by structure (never from wording): more-coming truth, a single ending, an ask exactly when the verdict was blocked on a
person, one open ask per key, an assignee who is a participant, no alike consecutive interims, bounds, ``done`` rebuilt from the receipts,
and the work-trace check on the real ``work_trace`` events. Keys are read by NAME from the environment or the two ``.env`` files and are
never printed or written."""
import argparse
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE.parents[2]))

KEEP = ("PATH", "HOME", "USER", "LANG", "LC_ALL", "TERM", "TMPDIR", "QAR_CLAUDE_PATH", "GOOGLE_AI_API_KEY", "OPENALEX_API_KEY")
KEY_NAMES = ("GOOGLE_AI_API_KEY", "OPENALEX_API_KEY")
ENV_FILES = (HERE.parents[3] / "quest-backend" / ".env", HERE.parents[3].parent / ".env")
EM_DASH = "—"

ASKER = "user-alice"
PARTICIPANTS = [{"user_id": "user-alice", "name": "Alice Nwosu", "email": "alice@example.org"},
                {"user_id": "user-bram", "name": "Bram Okafor", "email": "bram@example.org"}]

#: Fictional cards for the person's records. ``private`` ones belong to Alice and must not reach Bram in the group turn.
CARDS = [("marathon-april-quest", "Run a marathon in April: week 6 of 16, left knee sore after hills, physio visit, swim cross-training",
          ["marathon", "knee", "sore", "run", "long", "physio", "swim", "training", "week"]),
         ("candle-shop-launch", "Launch hand-poured candles shop: wax shipment slipped, soft launch 3 Nov, budget 2000 USD",
          ["candles", "shop", "launch", "wax", "budget"]),
         ("club-venue-booking", "Running club banquet: venue hold at Harbour Hall until Friday, deposit 1200 USD, Bram holds the club cheque book",
          ["club", "banquet", "venue", "deposit", "harbour", "hall", "cheque", "booking"])]

TURNS = {
    # (a) a goal that needs more than one pass: gather across sources, then synthesise.
    "research": dict(surface="solo", audience="owner", expect_ask=False, expect_interim="maybe", message=(
        "Compile a table of 6 different published studies from 2023 or later that measured VO2max change after high-intensity interval "
        "training in recreational runners. For every study give the authors, year, number of participants, the VO2max change with its "
        "unit, and a link that you opened and confirmed leads to the paper. Leave out any study you could not open. Reply with the table.")),
    # (b) a plain question: nothing to interim, nothing to ask.
    "simple": dict(surface="solo", audience="owner", expect_ask=False, expect_interim=False, message=(
        "In Python, what is the difference between a list and a tuple, and when should I use each?")),
    # (c) blocked on a choice only the person can make.
    "blocked": dict(surface="solo", audience="owner", expect_ask=True, expect_interim=False, message=(
        "Change my marathon quest to follow either the 12-week or the 16-week plan, whichever fits the trip I have booked "
        "in March. I have not told you the dates of the trip and you cannot know them, so please make the change once you have them.")),
    # (d) a team turn where the person who must answer is a teammate.
    "team": dict(surface="group", audience="shared", expect_ask=True, expect_interim=False, message=(
        "Secure the Harbour Hall banquet booking today by paying the 1200 USD deposit. I cannot release it: only Bram can, because he holds "
        "the club cheque book. Nothing else is needed from me.")),
}


def load_keys():
    """Read the two key names from the environment or the .env files into the allowlisted environment. Values are never printed."""
    found = {k: os.environ.get(k, "") for k in KEY_NAMES}
    for path in ENV_FILES:
        try:
            for line in path.read_text().splitlines():
                name, _, value = line.partition("=")
                if name.strip() in KEY_NAMES and not found[name.strip()] and value.strip():
                    found[name.strip()] = value.strip().strip('"').strip("'")
        except OSError:
            continue
    return found


def clean_env(root, keys):
    for key in [k for k in os.environ if k not in KEEP]:
        os.environ.pop(key)
    for name, value in keys.items():
        if value:
            os.environ[name] = value
    os.environ.update(QAR_MODEL_BACKEND="claude_cli", QAR_MODEL_FAST="haiku", QAR_MODEL_BALANCED="haiku", QAR_MODEL_QUALITY="haiku",
                      QAR_MODEL_BEST="haiku", QAR_DEEP_MODELS="haiku", QAR_DEEP_AUTO_MODEL="0", QAR_DEEP_WORKING_DIR=root,
                      QUEST_BASE_URL="http://quest.invalid", QUEST_API_KEY="offline", QAR_STANDARD_TOOLS="1", QAR_CORPUS_ROOT=root,
                      QAR_CARDS_BACKEND="local", QAR_CONVERSATION_SEARCH="false", QUEST_AI_RECENT_CONTEXT_DISABLED="1", QUEST_AI_ANTICIPATION="off")
    store = Path(root) / ".quest-context"
    store.mkdir(parents=True, exist_ok=True)
    for card_id, summary, keywords in CARDS:
        (store / f"{card_id}.json").write_text(json.dumps({"id": card_id, "keywords": keywords, "summary": summary, "conventions": [],
                                                           "usage_count": 1, "origin": "seed", "item_types": {"quest": 1}}))


class FakeDecisionSink:
    """The backend's contract, in memory: one OPEN ask per ``dedupe_key`` (a repeat returns the open one), and an assignee who is not a
    participant of the team is refused as the endpoint's 422 would."""

    def __init__(self, members):
        self.members = set(members)
        self.filed, self.calls = [], 0

    def escalate(self, esc):
        self.calls += 1
        import sys as _sys
        self.callers = getattr(self, "callers", []) + [[_sys._getframe(i).f_code.co_name for i in range(1, 5)]]
        if esc.assignee and esc.assignee not in self.members:
            raise ValueError(f"422: {esc.assignee} is not a member of the team")
        for row in self.filed:
            if esc.dedupe_key and row["dedupe_key"] == esc.dedupe_key:
                return row["id"]
        row = dict(id=f"decision-{len(self.filed) + 1}", dedupe_key=esc.dedupe_key, assigned_to=esc.assignee, notify_user_id=esc.notify_user_id,
                   summary=esc.summary, why_only_you=esc.why_only_you, options=esc.options, default_on_silence=esc.default_on_silence)
        self.filed.append(row)
        return row["id"]


class FakeQuestClient:
    """The in-process ``create_decision`` tool's client, backed by the same in-memory sink (dedupe, member check). Reads return nothing."""

    def __init__(self, sink):
        self.sink = sink

    def list_open_decisions_for_quest(self, quest_id):
        return []

    def list_assignable_members(self, quest_id):
        return [dict(p) for p in PARTICIPANTS]

    def create_decision(self, summary, **kw):
        from quest_ai_runner.core.adapters_values import Escalation
        try:
            ident = self.sink.escalate(Escalation(summary=summary, kind=kw.get("kind", "approve"), quest_id=kw.get("quest_id"),
                                                  assignee=kw.get("assignee_user_id"), notify_user_id=kw.get("notify_user_id"),
                                                  default_on_silence="hold", dedupe_key=None))
        except ValueError as e:
            raise RuntimeError(str(e))
        return {"decision_id": ident}


def start_quest_stub(sink):
    """A loopback stand-in for the Quest API so a deep worker that raises an ask itself (an out-of-process call) reaches the same in-memory
    sink: POST .../decisions files an ask (dedupe and member check as the backend), every other path answers an empty success."""
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Handler(BaseHTTPRequestHandler):
        def reply(self, code, body):
            raw = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):  # noqa: N802
            self.reply(200, [] if "decisions" in self.path or "members" in self.path else {})

        def do_POST(self):  # noqa: N802
            size = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(size) or b"{}")
            except ValueError:
                body = {}
            if self.path.rstrip("/").endswith("/decisions"):
                from quest_ai_runner.core.adapters_values import Escalation
                try:
                    ident = sink.escalate(Escalation(summary=str(body.get("summary") or ""), kind=str(body.get("kind") or "approve"),
                                                     quest_id=body.get("quest_id"), assignee=body.get("assigned_to_user_id") or body.get("assignee_user_id"),
                                                     notify_user_id=body.get("notify_user_id"), default_on_silence="hold", dedupe_key=body.get("dedupe_key")))
                except ValueError as e:
                    return self.reply(422, {"detail": str(e)})
                return self.reply(200, {"id": ident, "decision_id": ident, "status": "open"})
            self.reply(200, {})

        def log_message(self, *a):  # noqa: D401
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def instrument(orch, record):
    """Record, from results, the verdicts the loop read and each interim decision the policy made."""
    from quest_ai_runner.core import orchestrator_deep_run as deep
    verify = orch._verify_goal

    def verified(*a, **k):
        out = verify(*a, **k)
        verdict = out[0] if isinstance(out, tuple) else out
        if isinstance(verdict, dict):
            import sys as _s
            record["log"].append({"step": "verdict", "caller": _s._getframe(1).f_code.co_name, **{key: verdict.get(key) for key in ("met", "blocker", "reason", "next_action", "question", "asked_of", "member_ref", "claims_unexecuted", "claims_unsupported")}})
        return out
    orch._verify_goal = verified
    send = deep.maybe_send_interim

    def sent(orch_, emit, ctx_meta, res, verdict, **k):
        receipts = list(getattr(res, "observations", None) or []) if getattr(res, "observations_reported", False) else []
        why = send(orch_, emit, ctx_meta, res, verdict, **k)
        record["log"].append({"step": "interim_decision", "reason": why, "attempt": k.get("attempt"), "blocker": k.get("blocker"),
                              "receipts": receipts, "coverage": getattr(res, "coverage", None),
                              "flagged": bool(verdict.get("claims_unexecuted") or verdict.get("claims_unsupported") or getattr(res, "claim_corrected", False)),
                              "output": (res.output or "")[:4000], "at": time.monotonic()})
        return why
    deep.maybe_send_interim = sent
    ask = deep.file_blocked_ask

    def asked(orch_, emit, ctx_meta, res, verdict, **k):
        filed = ask(orch_, emit, ctx_meta, res, verdict, **k)
        record["log"].append({"step": "ask_attempt", "filed": filed, "question": k.get("question")})
        return filed
    deep.file_blocked_ask = asked


def run_turn(name, interim_replies):
    spec = TURNS[name]
    root = tempfile.mkdtemp(prefix="qar_interim_")
    clean_env(root, load_keys())
    from quest_ai_runner.cli import _config_from_env
    from quest_ai_runner.config import build_orchestrator
    from quest_ai_runner.core.adapters_sinks import StreamSink
    sink = FakeDecisionSink([p["user_id"] for p in PARTICIPANTS])
    stub = start_quest_stub(sink)
    os.environ["QUEST_BASE_URL"] = f"http://127.0.0.1:{stub.server_address[1]}"
    from quest_ai_runner.core import tools as core_tools
    core_tools.set_quest_client_factory(lambda env: FakeQuestClient(sink))
    cfg = _config_from_env()
    cfg.escalation = sink
    cfg.orchestrator.interim_replies = bool(interim_replies)
    orch = build_orchestrator(cfg)
    record = {"log": []}
    instrument(orch, record)
    frames = []
    meta = {"work_trace": {"audience": spec["audience"]}, "asker_user_id": ASKER, "user_id": ASKER, "participants": PARTICIPANTS,
            "conv_id": f"conv-{name}", "turn_id": f"msg-{name}"}
    started = time.time()
    result = orch.run(spec["message"], sink=StreamSink(frames.append), context_meta=meta)
    seconds = round(time.time() - started, 1)
    reply = str(getattr(result, "text", "") or "") or "\n\n".join(
        str(getattr(d, "output", "") or "").strip() for d in (getattr(result, "deep_results", None) or []) if str(getattr(d, "output", "") or "").strip())
    messages = []
    for f in frames:
        kind = f.get("type")
        if kind == "interim":
            messages.append({"kind": "interim", "text": f.get("text") or "", "data": f.get("data") or {}})
        elif kind == "decision":
            messages.append({"kind": "ask", "text": f.get("text") or "", "data": f.get("data") or {}, "decision_id": f.get("decision_id")})
    messages.append({"kind": "final", "text": reply})
    return dict(id=f"INTERIM-REAL-{name.upper()}", name=name, arm="new" if interim_replies else "today", surface=spec["surface"],
                audience=spec["audience"], message=spec["message"], expect_ask=spec["expect_ask"], expect_interim=spec["expect_interim"],
                messages=messages, frame_types=[f.get("type") for f in frames], log=record["log"],
                asks_filed=sink.filed, callers=getattr(sink, 'callers', []), sink_calls=sink.calls, decision_id=getattr(result, "decision_id", None),
                events=[f["data"] for f in frames if f.get("type") == "work_trace"], seconds=seconds,
                private_titles=[c[1][:60] for c in CARDS] if spec["surface"] == "group" else [],
                source="real run on the Claude Code subscription (haiku), real web search, seeded fictional card store, in-memory decision sink")


def check(case):
    """Structural truthfulness problems of one real turn. Empty list = every check passed. Reads the record, never the wording."""
    from quest_ai_runner.core.interim_policy import done_text
    problems = []
    log, messages = case["log"], case["messages"]
    interims = [m for m in messages if m["kind"] == "interim"]
    asks = [m for m in messages if m["kind"] == "ask"]
    # more-coming truth: after every sent interim the loop read another verdict, and a final or ask came after it.
    sent_at = [i for i, e in enumerate(log) if e["step"] == "interim_decision" and e["reason"] == "send"]
    for i in sent_at:
        if not any(e["step"] in ("verdict", "ask_attempt") for e in log[i + 1:]) and not any(e["step"] == "interim_decision" for e in log[i + 1:]):
            problems.append("an interim said more is coming but the goal loop did not run again")
    if len(sent_at) != len(interims):
        problems.append(f"the policy sent {len(sent_at)} interims but {len(interims)} reached the stream")
    for m in interims:
        if m["data"].get("will_continue") is not True:
            problems.append("an interim does not carry will_continue")
    # single ending: exactly one final, at most one ask, and the ask is the ending when present.
    if len(asks) > 1:
        problems.append(f"{len(asks)} asks shown for one turn")
    if len(case["asks_filed"]) > 1:
        problems.append(f"{len(case['asks_filed'])} decision-requests filed for one turn")
    if not str(messages[-1].get("text") or "").strip():
        problems.append("the final message is empty")
    if messages[-1]["kind"] != "final":
        problems.append("the turn does not end with its final")
    # ask exactly when the verifier's own verdict was a block on a person with a question.
    blocked = any(e["step"] == "verdict" and e.get("blocker") == "needs_person" and str(e.get("question") or "").strip() for e in log)
    if bool(case["asks_filed"]) != blocked:
        problems.append(f"ask filed={bool(case['asks_filed'])} but the verdict was {'blocked' if blocked else 'not blocked on a person'}")
    if asks and not case["asks_filed"]:
        problems.append("an ask was shown with no decision-request row")
    if case["asks_filed"] and not asks:
        problems.append("a decision-request was filed but no ask reached the stream")
    # dedupe: the sink saw distinct keys only once each.
    keys = [row["dedupe_key"] for row in case["asks_filed"]]
    if len(keys) != len(set(keys)):
        problems.append("two open asks share one dedupe key")
    allowed = {p["user_id"] for p in PARTICIPANTS}
    for row in case["asks_filed"]:
        if row["assigned_to"] not in allowed:
            problems.append("an ask is assigned to someone who is not a participant")
        if row["notify_user_id"] != ASKER:
            problems.append("the asker is not told the answer")
    # no two consecutive interims alike; bounds.
    done = [" ".join(str(m["data"].get("done") or "").split()) for m in interims]
    if any(a == b for a, b in zip(done, done[1:])):
        problems.append("two consecutive interims show the same work")
    if len(interims) > 4:
        problems.append("more interims than the per-turn cap")
    # done built from receipts: rebuild it from the logged receipts, coverage and the flag, and compare.
    decisions = [e for e in log if e["step"] == "interim_decision" and e["reason"] == "send"]
    for e, m in zip(decisions, interims):
        want = done_text(receipts=e["receipts"], coverage=e["coverage"], output=e["output"], output_flagged=e["flagged"])
        if " ".join(want.split()) != " ".join(str(m["data"].get("done") or "").split()):
            problems.append("an interim's done is not the receipts-and-coverage rebuild")
        if e["flagged"] and e["output"].strip() and e["output"].strip() in str(m["data"].get("done") or ""):
            problems.append("a flagged worker claim reached an interim")
    # no em dash in text the reader sees that code or the policy wrote.
    for m in interims + asks:
        if EM_DASH in m["text"]:
            problems.append("an em dash in an interim or ask")
    # a quiet turn (spam check): the simple input must send no interim and no ask.
    if case["expect_interim"] is False and interims:
        problems.append("an interim on a turn that needed none")
    if case["expect_ask"] is False and case["asks_filed"]:
        problems.append("an ask on a turn that needed none")
    # privacy in the group turn: the asker's private cards must not appear in what other participants see.
    for title in case["private_titles"]:
        for m in interims + asks:
            if title.lower() in m["text"].lower():
                problems.append("a private card's text reached an interim or ask in the shared thread")
    return problems


def work_trace_problems(case):
    from work_trace_experience import arms
    if not case["events"]:
        return []
    shaped = dict(case, stage="real", kind="group" if case["surface"] == "group" else "records", reply=case["messages"][-1]["text"], calls=[])
    views = arms.views_for("trail", shaped)
    return arms.gate_problems(shaped, views) + arms.parity_problems(shaped, views)


def run_in_subprocess(name, interim_replies, timeout=1700):
    """Run one real turn in a child process (the run rebuilds the environment from an allowlist, so it must not run inside the
    caller) and return the record, whose ``problems`` and ``work_trace_problems`` are the structural checks."""
    import subprocess
    with tempfile.TemporaryDirectory() as folder:
        out = Path(folder) / "turn.json"
        done = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--turn", name, "--arm", "new" if interim_replies else "today",
                               "--out", str(out)], capture_output=True, text=True, timeout=timeout)
        if not out.exists():
            return {"problems": [f"the real turn did not finish: {(done.stderr or done.stdout)[-200:]}"], "work_trace_problems": [], "messages": []}
        return json.loads(out.read_text())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--turn", required=True, choices=sorted(TURNS))
    ap.add_argument("--arm", required=True, choices=("new", "today"))
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    case = run_turn(args.turn, args.arm == "new")
    case["problems"] = check(case)
    try:
        case["work_trace_problems"] = work_trace_problems(case)
    except Exception as e:  # noqa: BLE001
        case["work_trace_problems"] = [f"work trace check could not run: {type(e).__name__}: {e}"]
    Path(args.out).write_text(json.dumps(case, indent=1, default=str))
    print(f"{case['id']} [{case['arm']}]: {len(case['messages'])} messages, {len(case['asks_filed'])} asks, {case['seconds']}s, "
          f"problems={case['problems']} work_trace={case['work_trace_problems']} -> {args.out}")


if __name__ == "__main__":
    main()
