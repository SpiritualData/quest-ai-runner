"""Client, guards, stream parsing and whole-quest fingerprint for the ``prod_quest_chat`` arm (the production Quest AI chat driven over HTTP).

Kept apart from ``devclient.py`` on purpose: that module refuses to load for anything but the dev backend and reads the dev eval
account's file. This one allows ONLY the production API, ONLY the AI account and ONLY the 23 case ids (and exact input texts) of the
private poorly answered file. Everything here except ``ProdClient``'s network calls is a pure function and offline-tested
(``test_prodchat.py``).

Safety is by design and verification, not by a write guard (none is possible over HTTP): the client can only POST to three routes (create
a conversation, send a message to the conversation this run created, stop that conversation); every other call is a GET. A whole-quest
fingerprint (notes, goals, workflows, spec records, tasks, decision-requests, the account's chat conversations) is read before and after
a case, and any change the case's ``effect`` allow-list does not explain is an UNEXPECTED WRITE that fails the case.
"""
import fcntl
import hashlib
import random
import tempfile
from contextlib import contextmanager
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request

import os

# The target is consumer config (this repository is public): the deployment's wrapper exports these. With any of them unset the arm refuses to start.
#   QAR_PROD_BASE_URL (the one production API), QAR_PROD_EXPECT_ACCOUNT (email), QAR_PROD_EXPECT_USER_ID, QAR_PROD_QUEST_ID, QAR_PROD_TEAM_ID
def target(name):
    value = (os.environ.get(name) or "").strip()
    if not value:
        raise GuardRefusal(f"prod_quest_chat: {name} is not set; refusing to run")
    return value.rstrip("/")


def environment_label():
    return f"production Quest AI chat (as {target('QAR_PROD_EXPECT_ACCOUNT')})"


CASE_MODEL_CALL_CAP = 60                   # model calls the stream reports (one "tokens" frame per call)
CASE_SECONDS_CAP = 360                     # 6 minutes per case


class RateLimited(RuntimeError):
    """The API kept answering 429 after the backoff budget: the run is 'not run: rate limited', never a failure of the product."""


MAX_BACKOFF = 90          # seconds of waiting on 429 answers before a call gives up


def backoff_delay(attempt, retry_after=None, jitter=0.0, remaining=MAX_BACKOFF):
    """Seconds to wait before retry ``attempt`` (0-based): the server's Retry-After when it sent one, else 2, 4, 8 ... up to 30, plus up to 25 percent jitter; never
    more than the budget ``remaining``."""
    base = float(retry_after) if retry_after else min(30.0, 2.0 ** (attempt + 1))
    return max(0.0, min(remaining, base * (1 + 0.25 * jitter)))


class GuardRefusal(SystemExit):
    """A guard stopped the run before anything was sent."""



# ---------------------------------------------------------------------------------------------------------------- guards

def check_base_url(url, prod_base=None):
    """Only the production API. Anything else (dev, a typo, a look-alike host) is refused."""
    prod_base = prod_base or target("QAR_PROD_BASE_URL")
    if (url or "").strip().rstrip("/") != prod_base:
        raise GuardRefusal(f"prod_quest_chat: base URL {url!r} is not {prod_base}; refusing to run")
    return prod_base


def check_account(who, expected_email=None, expected_id=None):
    """The identity call (``GET /api/teams/whoami``; ``/api/auth/me`` rejects the lane key) must say the AI account: both the user id and the email."""
    expected_email = (expected_email or target("QAR_PROD_EXPECT_ACCOUNT")).lower()
    expected_id = expected_id or target("QAR_PROD_EXPECT_USER_ID")
    who = who or {}
    if str(who.get("user_id") or who.get("id") or "") != expected_id or str(who.get("email") or "").strip().lower() != expected_email:
        shown = {k: who.get(k) for k in ("user_id", "id", "email", "error") if who.get(k)}
        raise GuardRefusal(f"prod_quest_chat: this key acts as {shown or 'an unknown account'}, not {expected_email} ({expected_id}); refusing to run")
    return {"user_id": expected_id, "email": expected_email}


def load_allowed_cases(path):
    """``{case_id: input_text}`` of the private poorly answered file: the only inputs this arm will ever send."""
    allowed = {}
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                row = json.loads(line)
                allowed[row["case_id"]] = (row.get("input_text") or "").strip()
    return allowed


def load_allowed_histories(path):
    """``{case_id: [(role, text)]}`` of the prior turns the private poorly answered file gives each case: the only prior turns this arm will ever send."""
    approved = {}
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                row = json.loads(line)
                approved[row["case_id"]] = [(str(t.get("role") or "").lower(), str(t.get("text") or "").strip()) for t in row.get("history") or [] if isinstance(t, dict)]
    return approved


def check_history(case, approved):
    """The user turns a case seeds before its input, refused unless they are exactly the file's. Returns the user texts to send, in order."""
    given = [(str(t.get("role") or "").lower(), str(t.get("text") or "").strip()) for t in case.get("history") or [] if isinstance(t, dict)]
    if given != approved.get(case.get("id"), []):
        raise GuardRefusal(f"prod_quest_chat: the prior turns of {case.get('id')} are not the approved ones; refusing to send them to production")
    return [text for role, text in given if role == "user"]


def check_case(case, allowed):
    """The case id must be one of the file's and the text sent must be exactly its input text. Any other input is refused."""
    case_id = case.get("id")
    message = (case.get("message") or (case.get("messages") or [""])[0] or "").strip()
    if case_id not in allowed:
        raise GuardRefusal(f"prod_quest_chat: {case_id!r} is not one of the {len(allowed)} approved case ids; refusing to send it to production")
    if message != allowed[case_id]:
        raise GuardRefusal(f"prod_quest_chat: the text for {case_id} is not its approved input text; refusing to send it to production")
    return message


# ---------------------------------------------------------------------------------------------------------------- client

#: How each kind of item a run created is undone, as the AI account, by routes that only take the item back (no resolution, no learning, no rejection).
UNDO_ROUTES = (("decisions", r"/api/teams/decisions/([\w-]+)/withdraw", "POST"), ("workflows", r"/api/workflows/([\w-]+)", "DELETE"),
               ("tasks", r"/api/assistant-tasks/([\w-]+)", "DELETE"), ("tasks", r"/api/assistant-tasks/([\w-]+)/undo", "POST"), ("notes", r"/api/quests/{quest}/notes/([\w-]+)", "DELETE"))


class ProdClient:
    """Bearer-key HTTP client for production. GETs are free; the only writes it will make are the three below, on the conversation it created."""

    def __init__(self, base, key, quest_id=None, team_id=None, timezone=""):
        self.base = check_base_url(base)
        self.quest_id = quest_id or target("QAR_PROD_QUEST_ID")
        self.team_id = team_id or target("QAR_PROD_TEAM_ID")
        self.key = key
        self.timezone = timezone
        self.own_conversations = set()
        self.sleep, self.jitter = time.sleep, random.random
        self.removable = set()           # ("decisions"|"tasks"|"workflows"|"notes", id) this run created and may undo; nothing else is ever touched

    def request_headers(self, accept="application/json"):
        return {"Authorization": f"Bearer {self.key}", "Content-Type": "application/json", "Accept": accept}

    def send_request(self, method, path, body=None, params=None, timeout=60):
        """JSON call. Anything but a GET must be one of the three allowed writes (``allow_write``)."""
        if method != "GET":
            self.allow_write(method, path)
        url = self.base + path + ("?" + urllib.parse.urlencode(params) if params else "")
        waited = 0.0
        for attempt in range(1000):
            req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None, method=method, headers=self.request_headers())
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    raw = resp.read().decode()
                    return json.loads(raw) if raw.strip() else None
            except urllib.error.HTTPError as e:
                if e.code != 429:
                    raise RuntimeError(f"{method} {path}: {e.code} {e.read().decode()[:300]}") from None
                header = e.headers.get("Retry-After") if e.headers else None
                try:
                    retry_after = float(header) if header else None
                except ValueError:
                    retry_after = None
                if waited >= MAX_BACKOFF:
                    raise RateLimited(f"{method} {path}: 429 Too many requests after waiting {waited:.0f}s") from None
                delay = backoff_delay(attempt, retry_after, self.jitter(), MAX_BACKOFF - waited) or 1.0
                self.sleep(delay)
                waited += delay

    def allow_write(self, method, path):
        if method == "POST" and path == "/api/quest-ai/conversations":
            return
        match = re.fullmatch(r"/api/quest-ai/conversations/([\w-]+)/(messages/stream|stop)", path)
        if method == "POST" and match and match.group(1) in self.own_conversations:
            return
        for category, pattern, verb in UNDO_ROUTES:
            found = re.fullmatch(pattern.replace("{quest}", re.escape(self.quest_id)), path)
            if method == verb and found and (category, found.group(1)) in self.removable:
                return
        raise GuardRefusal(f"prod_quest_chat: {method} {path} is not an allowed write (only: create a conversation, send to and stop the conversation this run created)")

    def whoami(self):
        try:
            return self.send_request("GET", "/api/teams/whoami") or {}
        except Exception as e:  # noqa: BLE001
            return {"error": f"{type(e).__name__}: {e}"}

    def create_conversation(self):
        """A fresh chat pinned to the quest the way the Quest app opens a quest chat: ``quest_ids`` plus the quest's team."""
        body = self.send_request("POST", "/api/quest-ai/conversations", {"quest_ids": [self.quest_id], "team_id": self.team_id})
        conv = (body or {}).get("conversation_id") or (body or {}).get("id") or ((body or {}).get("data") or {}).get("id")
        if not conv:
            raise RuntimeError(f"create conversation returned no id: {str(body)[:200]}")
        self.own_conversations.add(conv)
        return conv

    def seed_history(self, conv, user_texts, started):
        """Send the approved prior user turns of a case into the conversation this run created, each as a real turn the server answers, before the case's own input.
        The chat has no route that stores a recorded assistant reply, so the prior replies are the server's own; an in-process arm seeds the recorded ones."""
        for text in user_texts:
            for _event in self.stream_turn(conv, text, started):
                pass

    def stop(self, conv):
        try:
            self.send_request("POST", f"/api/quest-ai/conversations/{conv}/stop", {})
        except Exception:  # noqa: BLE001 -- best effort, the turn may already be over
            pass

    def stream_turn(self, conv, content, started, auto_run=True, max_seconds=CASE_SECONDS_CAP, max_calls=CASE_MODEL_CALL_CAP):
        """Send one message and yield every SSE event as it arrives (``parse_sse`` on the raw lines), each stamped ``t`` (seconds since ``started``).
        Stops, and asks the server to stop the turn, at the time cap or when the stream has reported ``max_calls`` model calls."""
        path = f"/api/quest-ai/conversations/{conv}/messages/stream"
        self.allow_write("POST", path)
        payload = {"content": content, "auto_run": auto_run}
        if self.timezone:
            payload["timezone"] = self.timezone
        req = urllib.request.Request(self.base + path, data=json.dumps(payload).encode(), method="POST", headers=self.request_headers("text/event-stream"))
        calls = 0
        try:
            with urllib.request.urlopen(req, timeout=max_seconds) as resp:
                for event in parse_sse(raw.decode("utf-8", "replace") for raw in resp):
                    event["t"] = round(time.time() - started, 1)
                    yield event
                    calls += model_calls_in(event)
                    if calls >= max_calls:
                        yield {"event": "_capped", "text": f"stopped at {max_calls} model calls", "t": round(time.time() - started, 1)}
                        self.stop(conv)
                        return
                    if time.time() - started > max_seconds:
                        yield {"event": "_capped", "text": f"stopped at {max_seconds}s", "t": round(time.time() - started, 1)}
                        self.stop(conv)
                        return
        except urllib.error.HTTPError as e:
            yield {"event": "_http_error", "text": f"{e.code}: {e.read().decode()[:300]}", "t": round(time.time() - started, 1)}
        except Exception as e:  # noqa: BLE001 -- a read timeout ends the turn here
            self.stop(conv)
            yield {"event": "_error", "text": f"{type(e).__name__}: {e}", "t": round(time.time() - started, 1)}


# ---------------------------------------------------------------------------------------------------------------- stream parsing

def parse_sse(lines):
    """Events (dicts) from the raw lines of a ``text/event-stream`` body: ``data: {json}`` lines; ``[DONE]`` and blank data are skipped, bad JSON is kept as ``_unparsed``."""
    for raw in lines:
        line = raw.strip()
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if not data or data == "[DONE]":
            continue
        try:
            event = json.loads(data)
        except json.JSONDecodeError:
            event = {"event": "_unparsed", "text": data[:200]}
        if isinstance(event, dict):
            yield event


def event_name(event):
    return event.get("event") or event.get("type") or ""


def model_calls_in(event):
    """One model call per cumulative ``tokens`` frame that is not the terminal count (the terminal ``final`` frame repeats the last total)."""
    return 1 if event_name(event) == "tokens" and not (event.get("data") or {}).get("final") else 0


def frames_of(events):
    """The stream's events as the frames the shared helpers read (``type`` = the event name; ``prod_probes.messages_of`` and ``judge.build_evidence`` take both)."""
    return [dict(e, type=event_name(e)) for e in events]


def usage_of(events):
    """``{"model_calls", "tokens_in", "tokens_out"}`` from the stream: the cumulative ``tokens`` frames (the last wins), else a ``usage`` object on any event (the ``done``
    frame's ``data`` included) that carries ``tokens_in``/``tokens_out`` (or ``input_tokens``/``output_tokens``). The production stream may send neither; the counts are then 0."""
    tokens = [e.get("data") or {} for e in events if event_name(e) == "tokens"]
    last = tokens[-1] if tokens else {}
    tokens_in, tokens_out = int(last.get("tokens_in") or 0), int(last.get("tokens_out") or 0)
    if not tokens_in and not tokens_out:
        for e in events:
            for holder in (e, e.get("data") or {}):
                usage = holder.get("usage") if isinstance(holder, dict) else None
                if isinstance(usage, dict):
                    tokens_in = int(usage.get("tokens_in") or usage.get("input_tokens") or tokens_in or 0)
                    tokens_out = int(usage.get("tokens_out") or usage.get("output_tokens") or tokens_out or 0)
    return {"model_calls": sum(model_calls_in(e) for e in events), "tokens_in": tokens_in, "tokens_out": tokens_out}


def route_of(events):
    """How the chat handled the turn, from the stream's own frame types: deep (handed to a worker), delegated, or answered inline."""
    names = [event_name(e) for e in events]
    if "delegated" in names:
        return "delegated"
    plans = [(e.get("data") or {}).get("action") for e in events if event_name(e) in ("plan", "replan")]
    if any(str(a or "").lower() in ("deep", "delegate") for a in plans):
        return "deep"
    return "inline" if "done" in names else "none"


def tool_calls_of(events):
    """The tools the turn called, in order, from the ``exec`` frames (tool name and short args)."""
    out = []
    for e in events:
        if event_name(e) != "exec":
            continue
        d = e.get("data") or {}
        tool = d.get("tool")
        if tool and d.get("phase") in (None, "executing", "code", "done", "output"):
            entry = f"{tool}" + (f" {json.dumps(d.get('args') or d.get('arguments') or d.get('input'), default=str)[:100]}" if (d.get("args") or d.get("arguments") or d.get("input")) else "")
            if entry not in out:
                out.append(entry)
    return out


def reads_from_stream(events, per_item=2500):
    """What the turn read, as ``[{"source", "text"}]`` evidence: each ``read`` frame (sources and locators), each ``exec`` output, and the
    explanation trace; the judge and the unsupported-figures check use this in place of the in-process gathered observations."""
    items = []
    for e in events:
        name, d = event_name(e), e.get("data") or {}
        if name == "read":
            text = json.dumps({k: d.get(k) for k in ("reads", "sources", "locators") if d.get(k)}, default=str)
            items.append({"source": f"read step {d.get('step')}", "text": text[:per_item]})
        elif name == "exec" and (d.get("output") or d.get("result")):
            items.append({"source": f"tool {d.get('tool') or 'exec'} output", "text": str(d.get("output") or d.get("result"))[:per_item]})
        elif name == "explanation":
            items.append({"source": "explanation trace", "text": json.dumps(d, default=str)[:per_item]})
    return items


def stored_messages(conversation):
    """The conversation read back: ``[{"role", "kind", "timestamp", "text"}]`` for every stored message."""
    return [{"role": m.get("role"), "kind": m.get("kind"), "timestamp": str(m.get("timestamp") or ""), "text": str(m.get("content") or "")[:1500]}
            for m in (conversation or {}).get("messages") or []]


# ---------------------------------------------------------------------------------------------------------------- fingerprint

CATEGORIES = ("notes", "goals", "workflows", "spec_records", "tasks", "decisions", "conversations")
KEEP_REMOVED = ("notes", "goals", "workflows", "spec_records")      # lists that are complete reads; a task or conversation list is a window, so a drop-off is not a deletion


def digest(*parts):
    return hashlib.sha1(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()[:12]


def task_in_quest(task, quest_id):
    """False only when the task names a quest and it is not ``quest_id`` (the fields a task may carry it in: ``quest_id``, ``goal_id``, ``quest_ids``). A task that names none is kept: it cannot be
    told apart, and a task the run's own conversation created must never be dropped for a field the listing lacks."""
    named = {str(v) for k in ("quest_id", "goal_id") for v in [task.get(k)] if v} | {str(q) for q in task.get("quest_ids") or []}
    return not named or quest_id in named


def read_quest_tasks(client):
    """The quest's tasks whoever owns them (``include_quest_tasks``: whoever can see a quest sees its tasks), newest ``TASK_PAGE``. A task the chat creates can be owned by another
    account than the one this run acts as (created for the quest's AI, from the owner's conversation); an owner-scoped list would never show it, and the lane would run it during a later case."""
    body = client.send_request("GET", "/api/assistant-tasks", params={"quest_id": client.quest_id, "limit": TASK_PAGE, "include_quest_tasks": "true"})
    tasks = body.get("tasks") if isinstance(body, dict) else body
    return [t for t in tasks or [] if isinstance(t, dict) and task_in_quest(t, client.quest_id)]


def fingerprint_from(reads):
    """The fingerprint from raw reads ``{name: payload}`` (a pure function): ``{category: {id: {"digest", "label", ...}}}``."""
    fp = {c: {} for c in CATEGORIES}
    for note in reads.get("notes") or []:
        nid = note.get("note_id") or note.get("id")
        fp["notes"][nid] = {"digest": digest(note.get("text"), note.get("updated_at")), "label": str(note.get("text") or "")[:80], "author": note.get("author_name"),
                                  "author_id": note.get("author_id"), "created_at": note.get("created_at")}
    for goal in reads.get("goals") or []:
        fp["goals"][goal.get("id")] = {"digest": digest(goal.get("name"), goal.get("completed"), goal.get("criteria"), goal.get("description"), goal.get("deadline"), goal.get("scheduled_date")),
                                       "label": str(goal.get("name") or "")[:80], "kept": str(goal.get("description") or "").strip().startswith("Kept by Quest AI:")}
    for wf in reads.get("workflows") or []:
        fp["workflows"][wf.get("workflowId")] = {"digest": digest(wf.get("title"), wf.get("status"), wf.get("updatedAt"), wf.get("steps")), "label": str(wf.get("title") or "")[:80], "created_at": wf.get("createdAt")}
    for rec in reads.get("spec_records") or []:
        fp["spec_records"][rec.get("recordId")] = {"digest": digest(rec.get("revision"), rec.get("updatedAt"), rec.get("fields")), "label": f"{rec.get('type')}"}
    for task in reads.get("tasks") or []:
        fp["tasks"][task.get("task_id") or task.get("id")] = {"digest": "", "label": str(task.get("title") or task.get("text") or "")[:80], "conv_id": task.get("conv_id"), "created_by": task.get("created_by"),
                                                               "owner": task.get("user_id") or task.get("owner_id"), "status": task.get("status"),
                                                               "source": task.get("source"), "created_at": task.get("created_at")}
    for dec in reads.get("decisions") or []:
        fp["decisions"][dec.get("decision_id")] = {"digest": "", "label": str(dec.get("summary") or "")[:80], "conv_id": dec.get("conv_id"), "kind": dec.get("kind"), "status": dec.get("status"),
                                                   "created_by": dec.get("created_by"), "created_at": dec.get("created_at")}
    for conv in reads.get("conversations") or []:
        fp["conversations"][conv.get("id")] = {"digest": "", "label": str(conv.get("title") or "")[:80]}
    return fp


LOCK_NAME = "prod-fingerprint-read.lock"
TASK_PAGE = 200      # newest tasks of the quest; a fingerprint only needs what is new, and a smaller page is kinder to the rate limit


@contextmanager
def fingerprint_lock(directory=None, blocking=True):
    """One fingerprint read at a time across processes (parallel lane-arm runs share the account's rate limit): an exclusive file lock in the scratch dir."""
    import os
    # Named per system user: in a sticky /tmp one user cannot open a lock file another user created.
    name = LOCK_NAME.replace(".lock", f"-{os.getuid()}.lock")
    path = os.path.join(directory or os.environ.get("QAR_PROD_LOCK_DIR") or tempfile.gettempdir(), name)
    with open(path, "a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        try:
            yield path
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def read_fingerprint(client, own_conv=None):
    """Read the quest as the account sees it right now (GETs only), holding the cross-process lock."""
    with fingerprint_lock():
        return read_fingerprint_unlocked(client, own_conv)


def read_fingerprint_unlocked(client, own_conv=None):
    def read(path, key=None, params=None):
        try:
            body = client.send_request("GET", path, params=params)
        except RateLimited:
            raise
        except Exception as e:  # noqa: BLE001 -- an unreadable category must fail loudly, never look like "unchanged"
            raise RuntimeError(f"fingerprint read failed: {path}: {e}") from None
        if key and isinstance(body, dict):
            return body.get(key) or []
        return body if isinstance(body, list) else []
    quest_id, team_id = client.quest_id, client.team_id
    goals_body = client.send_request("GET", f"/api/teams/{team_id}/quests/{quest_id}/goals") or {}
    reads = {
        "notes": read(f"/api/quests/{quest_id}/notes"),
        "goals": [g for group in goals_body.get("period_groups") or [] for g in group.get("goals") or []],
        "workflows": read("/api/workflows", "items") + read(f"/api/quests/{quest_id}/workflows", "items"),
        "spec_records": read(f"/api/intents/specs/quest/{quest_id}", "records"),
        "tasks": read_quest_tasks(client),
        "decisions": read("/api/teams/decisions/for-user", "decisions") + read("/api/teams/decisions/for-quest", "decisions", {"quest_id": quest_id}),
        "conversations": read("/api/quest-ai/conversations", None, {"limit": 500}),
    }
    return fingerprint_from(reads)


def diff_fingerprints(before, after, own_conv=None):
    """Every change between two fingerprints, as ``{"category", "change": added|removed|changed, "id", "label", ...}``. The run's own conversation is the one thing left out."""
    changes = []
    for cat in CATEGORIES:
        old, new = before.get(cat, {}), after.get(cat, {})
        for item_id, meta in new.items():
            if cat == "conversations" and item_id == own_conv:
                continue
            if item_id not in old:
                changes.append(dict(meta, category=cat, change="added", id=item_id))
            elif meta.get("digest") != old[item_id].get("digest"):
                changes.append(dict(meta, category=cat, change="changed", id=item_id, before_label=old[item_id].get("label", ""), before_kept=bool(old[item_id].get("kept"))))
        if cat in KEEP_REMOVED:
            changes += [dict(old[i], category=cat, change="removed", id=i) for i in old if i not in new]
    return changes


ALLOW_WORDS = {"notes": ("note",), "goals": ("goal",), "workflows": ("workflow",), "spec_records": ("spec", "record"), "tasks": ("task", "delegate", "queue"),
               "decisions": ("decision", "approval", "ask"), "conversations": ("conversation",)}


def allowed_categories(effect):
    """The fingerprint categories a case's ``effect`` allow-list explains, matched by meaning (the tool name, the method and path and the arguments
    are searched for the category's words). A case with no allow entries explains nothing."""
    text = json.dumps((effect or {}).get("allow") or [], default=str).lower() if isinstance(effect, dict) else ""
    return {cat for cat, words in ALLOW_WORDS.items() if any(w in text for w in words)} if text and text != "[]" else set()


def allowed_title_starts(effect):
    """The goal title starts a case's allowed calls name (``title~`` / ``title``), lower case."""
    out = []
    for rule in (effect or {}).get("allow") or [] if isinstance(effect, dict) else []:
        for key, want in (rule.get("args") or {}).items():
            if key.rstrip("~") == "title":
                out += [str(w).strip().casefold() for w in (want if isinstance(want, list) else [want])]
    return out


def goal_change_explained(change, effect, titles=()):
    """A changed existing goal is explained only when it was one the case may change: it was a goal Quest AI keeps (its description began with the marker, or its title ended in the keyed
    "next step: week of …" pattern) before the run, or its title BEFORE the run starts with a title the case's rule allows. A title that merely equals the title of an allowed call is
    no reason (``titles`` is accepted for old callers and not used): a rule with no title constraint allows creating a goal, never changing one."""
    before = str(change.get("before_label") or "").strip()
    return bool(change.get("before_kept")) or bool(re.search(r"next step: week of \d{4}-\d{2}-\d{2}\s*$", before)) or any(p and before.casefold().startswith(p) for p in allowed_title_starts(effect))


def executed_categories(executed):
    """The fingerprint categories the turn's EXECUTED writes could have created, from the dry-run guard's record of what it let through (``RECORDER.performed``:
    ``tool <name> ...`` and ``quest api <METHOD> <path>``). Matched by word start on the tool name and path words, so ``add_quest_note`` reaches notes and
    ``/api/assistant-tasks`` reaches tasks, and ``task`` never reaches decisions."""
    tokens = [t for line in executed or [] for t in re.split(r"[^a-z0-9]+", str(line).lower()) if t]
    return {cat for cat, words in ALLOW_WORDS.items() if any(t.startswith(w) for t in tokens for w in words)}


DERIVED_PREFIX = "derived:"      # a spec record the backend computes from the quest's own fields on every read; it is not a stored row, so it moves when the quest does


def derived_change_possible(executed):
    """True when an executed write could have moved a quest-derived record: a write to the spec records themselves or to the quest's own fields (an update of the quest or its intent)."""
    tokens = [t for line in executed or [] for t in re.split(r"[^a-z0-9]+", str(line).lower()) if t]
    if "spec_records" in executed_categories(executed):
        return True
    return any(t.startswith("intent") for t in tokens) or any(a == "update" and b.startswith("quest") for a, b in zip(tokens, tokens[1:])) or any(a.startswith("quest") and b == "update" for a, b in zip(tokens, tokens[1:]))


def split_concurrent(changes, executed=None):
    """``(this turn's possible changes, concurrent changes)``. Only an in-process arm can tell: every real write it makes goes through the dry-run guard, so an ADDED item
    whose category no executed write could have created is provably not this turn's (another lane writing as the same account). The same holds for a CHANGED quest-derived spec
    record (``derived:...``): the backend computes it from the quest's fields, so it moves when someone else edits the quest, and it is this turn's only when the turn executed a write
    that could reach those fields. ``executed=None`` (the HTTP arm, which cannot see the turn's calls) keeps every change strict. Any other changed or removed item is never called concurrent."""
    if executed is None:
        return list(changes), []
    could = executed_categories(executed)
    derived_possible = derived_change_possible(executed)
    mine, other = [], []
    for c in changes:
        added_elsewhere = c["change"] == "added" and c["category"] not in could
        derived_elsewhere = c["change"] == "changed" and c["category"] == "spec_records" and str(c.get("id") or "").startswith(DERIVED_PREFIX) and not derived_possible
        (other if added_elsewhere or derived_elsewhere else mine).append(c)
    return mine, other


def concurrent_lines(changes):
    """Readable ``CONCURRENT WRITE`` lines for the additions that are not this turn's: reported, never counted as the turn's and never touched by cleanup."""
    out = []
    for c in changes:
        extra = ", ".join(f"{k}={c[k]}" for k in ("author", "conv_id", "source", "created_at", "kind") if c.get(k))
        out.append(f"CONCURRENT WRITE (not this turn's, no executed write could have made it): {c['category']} {c['change']} {c['id']} {c.get('label', '')!r}" + (f" ({extra})" if extra else ""))
    return out


def unexpected_writes(changes, effect=None, zero_writes=False, titles=()):
    """The changes the case's allowed writes do not explain. A zero-writes case explains none; otherwise a change is explained when its category is
    one the ``effect`` allow-list names, and a changed or removed GOAL only when it is a goal the case may change (``goal_change_explained``). Each is returned as a readable
    ``UNEXPECTED WRITE`` line."""
    allowed = set() if zero_writes else allowed_categories(effect)
    out = []
    for c in changes:
        if c["category"] in allowed and not (c["category"] == "goals" and c["change"] in ("changed", "removed") and not goal_change_explained(c, effect, titles)):
            continue
        extra = ", ".join(f"{k}={c[k]}" for k in ("author", "conv_id", "source", "created_at", "kind") if c.get(k))
        was = f" (was {c['before_label']!r}: a goal the case may not change)" if c.get("before_label") and c["category"] == "goals" else ""
        out.append(f"UNEXPECTED WRITE: {c['category']} {c['change']} {c['id']} {c.get('label', '')!r}{was}" + (f" ({extra})" if extra else ""))
    return out


# ---------------------------------------------------------------------------------------------------------------- cleanup: nothing a run creates needs a human to undo

def describe_undo(effect, zero_writes):
    """The line printed before a case runs: what it may create and how each kind is undone."""
    kept = sorted(allowed_categories(effect)) if not zero_writes else []
    undo = "; ".join(f"{cat}: {verb} {pattern.split('/api/')[1].replace('{quest}', '<quest>').replace('([\\w-]+)', '<id>')}" for cat, pattern, verb in UNDO_ROUTES)
    return (f"intended results kept (idempotent): {', '.join(kept) or 'none (a zero-writes case)'}; anything else this run creates is taken back as the AI account "
            f"({undo}); a change that cannot be taken back fails the case")


STOPWORDS = {"about", "after", "again", "could", "every", "should", "their", "there", "these", "those", "which", "would", "please", "quest", "write", "other", "where", "while", "being"}


def keywords_of(message):
    """The case's key words: its distinct words of five letters or more, minus common ones."""
    return sorted({w for w in re.findall(r"[a-z][a-z\-']{4,}", str(message or "").lower()) if w not in STOPWORDS})


def parse_time(value):
    """A timestamp (ISO text with or without a zone, or epoch seconds) as epoch seconds, else None. A zone-less text is UTC."""
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    import datetime
    try:
        moment = datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return (moment if moment.tzinfo else moment.replace(tzinfo=datetime.timezone.utc)).timestamp()


def strongly_this_turns(item, window, ai_user_id, evidence, words):
    """True only when the item passes every test that ties it to THIS turn: created inside the window ``(start, end + 30s)``, authored by the AI account
    when the record carries an author or creator, and either its id appears in the turn's own stream frames or its text shares at least two of the case's
    key words. An item with no author or creator field (a workflow) needs its id in the frames. Nothing is guessed."""
    created = parse_time(item.get("created_at"))
    if created is None or not (window[0] <= created <= window[1]):
        return False
    owner = item.get("author_id") or item.get("created_by")
    if owner is not None and owner != ai_user_id:
        return False
    if str(item["id"]).lower() in evidence:
        return True
    if owner is None:
        return False
    label = str(item.get("label") or "").lower()
    return sum(1 for w in words if w in label) >= 2


def plan_cleanup(changes, own_conv, window, ai_user_id, effect=None, zero_writes=False, evidence="", message="", titles=()):
    """Split the fingerprint changes into ``remove``, ``keep`` and ``left``. ``remove`` is only what is provably this run's and not the case's intended result: a
    decision or task carrying this run's conversation id, or a note, workflow, decision or task that passes ``strongly_this_turns``. ``keep`` is the intended,
    allowed result. ``left`` is everything else: a changed or removed item, an item with no route that takes it back (a goal, a spec record, another conversation),
    and any item created in the window that the tests do not tie to this turn, marked ``AMBIGUOUS: left`` (it may be a concurrent autopilot write by the same account;
    nothing is guessed, a human is told, and the case fails). ``window`` is ``(turn start, turn end + 30s)`` in epoch seconds."""
    allowed = set() if zero_writes else allowed_categories(effect)
    evidence, words = str(evidence or "").lower(), keywords_of(message)
    plan = {"remove": [], "keep": [], "left": [], "stop": []}
    for c in changes:
        cat = c["category"]
        if c["change"] == "changed" and cat == "goals" and cat in allowed and goal_change_explained(c, effect, titles):
            plan["keep"].append(c)          # the keyed goal the case allows, updated in place: that is the intended result, not something left behind
        elif c["change"] != "added":
            plan["left"].append(dict(c, why=f"{c['change']}: cannot be undone from here"))
        elif cat in allowed:
            plan["keep"].append(c)
            if cat == "tasks" and own_conv and c.get("conv_id") == own_conv:
                plan["stop"].append(c)          # the intended task is kept as a record, but cancelled: a queued one must never run in the next case's window
        elif cat in ("decisions", "tasks") and own_conv and c.get("conv_id") == own_conv:
            plan["remove"].append(c)
        elif cat in ("decisions", "tasks", "notes", "workflows") and strongly_this_turns(c, window, ai_user_id, evidence, words):
            plan["remove"].append(c)
        elif cat in ("decisions", "tasks", "notes", "workflows") and window[0] <= (parse_time(c.get("created_at")) or 0) <= window[1]:
            plan["left"].append(dict(c, why="AMBIGUOUS: left. Created in the turn window but not tied to this turn (it may be a concurrent write by the same account)"))
        else:
            plan["left"].append(dict(c, why="no route takes it back or it is not provably this run's"))
    return plan


def cancel_task(client, item_id):
    """Cancel a queued or running task through the stop route (open to everyone who can see the task, whoever owns it). True when it was cancelled, False when it had already ended."""
    client.removable.add(("tasks", item_id))
    try:
        client.send_request("POST", f"/api/assistant-tasks/{item_id}/undo", {})
        return True
    except GuardRefusal:
        raise
    except Exception as e:  # noqa: BLE001
        if "409" in str(e):
            return False
        raise


def run_cleanup(client, plan):
    """Take back what the plan says to remove, as the AI account. Returns ``{"withdrawn": [ids], "deleted": [ids], "cancelled": [ids], "left": [text], "kept": [ids]}``; an item whose
    removal fails is ``left``. A task is cancelled first (the stop route works across accounts, so it cannot run into a later case), then deleted when the account owns it. A task the case
    keeps as its intended result is only cancelled (``plan["stop"]``): ``cancelled`` lists those."""
    out = {"withdrawn": [], "deleted": [], "cancelled": [], "left": [f"{c['category']} {c['id']} ({c['why']})" for c in plan["left"]], "kept": [c["id"] for c in plan["keep"]]}
    for c in plan.get("stop") or []:
        try:
            if cancel_task(client, c["id"]):
                out["cancelled"].append(c["id"])
        except Exception as e:  # noqa: BLE001
            out["left"].append(f"tasks {c['id']} (cancel failed: {str(e)[:120]})")
    for c in plan["remove"]:
        cat, item_id = c["category"], c["id"]
        client.removable.add((cat, item_id))
        path, method = {"decisions": (f"/api/teams/decisions/{item_id}/withdraw", "POST"), "workflows": (f"/api/workflows/{item_id}", "DELETE"),
                        "tasks": (f"/api/assistant-tasks/{item_id}", "DELETE"), "notes": (f"/api/quests/{client.quest_id}/notes/{item_id}", "DELETE")}[cat]
        try:
            stopped = cat == "tasks" and cancel_task(client, item_id)
            try:
                client.send_request(method, path, {} if method == "POST" else None)
                (out["withdrawn"] if cat == "decisions" else out["deleted"]).append(item_id)
            except Exception:  # noqa: BLE001 -- a task owned by another account cannot be deleted from here; cancelled is enough
                if not stopped:
                    raise
                out["cancelled"].append(item_id)
        except Exception as e:  # noqa: BLE001
            out["left"].append(f"{cat} {item_id} (removal failed: {str(e)[:120]})")
    return out


def cleanup_line(result):
    cancelled = f"cancelled {result['cancelled']}, " if result.get("cancelled") else ""
    return f"cleanup: withdrawn {result['withdrawn'] or 'none'}, deleted {result['deleted'] or 'none'}, {cancelled}left {result['left'] or 'none'}, kept (the case's intended result) {result['kept'] or 'none'}"


# ---------------------------------------------------------------------------------------------------------------- autopilot window

TERMINAL_TASK = {"done", "failed", "cancelled", "completed", "error", "dismissed", "incomplete", "needs_you", "withdrawn"}


def autopilot_window(now, autopilot, tasks, minutes=10):
    """Why a write-capable case must not start now, else None. ``now`` is epoch seconds. A reason when: a task on the quest is active (neither queued nor finished),
    a queued task has no schedule (it is runnable now) or is scheduled up to ``minutes`` ahead (in the autopilot's time zone, or UTC when it has none), or the quest's
    daily autopilot run time falls in the next ``minutes`` or began within the last ``minutes``."""
    import datetime
    horizon = now + minutes * 60
    for task in tasks or []:
        status = str(task.get("status") or "").lower()
        if status in TERMINAL_TASK:
            continue
        if status != "queued":
            return f"autopilot window: task {task.get('task_id')} is {status}"
        date, clock = task.get("scheduled_date"), task.get("scheduled_time")
        if not date:
            if stale_queue(now, task):
                continue      # no runner has taken it in hours, so it will not start during a case; holding every case for it measures nothing
            return f"autopilot window: queued task {task.get('task_id')} has no schedule and is runnable now"
        for zone in [(autopilot or {}).get("run_timezone") or "UTC"]:      # a schedule is in the quest's own zone; UTC only when it has none
            when = local_epoch(date, clock or "00:00", zone)
            if when is not None and when <= horizon:
                return f"autopilot window: queued task {task.get('task_id')} is scheduled for {date} {clock} ({zone})"
    run_time, zone = (autopilot or {}).get("run_time"), (autopilot or {}).get("run_timezone") or "UTC"
    if run_time and (autopilot or {}).get("cadence"):
        for offset in (-1, 0, 1):
            day = (datetime.datetime.fromtimestamp(now, datetime.timezone.utc) + datetime.timedelta(days=offset)).date().isoformat()
            when = local_epoch(day, run_time, zone)
            if when is not None and now - minutes * 60 <= when <= horizon:
                return f"autopilot window: the daily autopilot run time {run_time} ({zone}) is within {minutes} minutes"
    return None


STALE_QUEUE_SECONDS = 2 * 3600      # a task with no schedule that sat queued this long is not about to run: no runner is taking it


def stale_queue(now, task, seconds=STALE_QUEUE_SECONDS):
    """Whether a queued task with no schedule has waited so long, untouched, that no runner is taking it (its team is not polled, or it
    needs a runner that does not exist). Such a task would hold every write-capable case forever; it is a routing fault to report, not a
    run about to start. Judged from the task's last change, else its creation."""
    import datetime
    stamp = str(task.get("updated_at") or task.get("created_at") or "")
    try:
        when = datetime.datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return False
    if when.tzinfo is None:
        when = when.replace(tzinfo=datetime.timezone.utc)
    return now - when.timestamp() > seconds


def stale_queued_tasks(now, tasks):
    """The ids of the queued, unscheduled tasks ``autopilot_window`` passes over as stale, for the run to name as a routing fault."""
    return [str(t.get("task_id")) for t in tasks or []
            if str(t.get("status") or "").lower() == "queued" and not t.get("scheduled_date") and stale_queue(now, t)]


def local_epoch(date, clock, zone):
    import datetime
    try:
        from zoneinfo import ZoneInfo
        naive = datetime.datetime.fromisoformat(f"{date}T{clock}")
        return naive.replace(tzinfo=ZoneInfo(zone)).timestamp()
    except Exception:  # noqa: BLE001
        return None


def wait_for_clear(client, now=time.time, sleep=time.sleep, max_wait=900, step=30, minutes=10):
    """None when no autopilot or lane run is active or due within ``minutes``; waits up to ``max_wait`` seconds in ``step`` sleeps; the last reason when it never clears."""
    waited = 0
    while True:
        with fingerprint_lock():
            state = client.send_request("GET", f"/api/quests/{client.quest_id}/state") or {}
            tasks = read_quest_tasks(client)
        autopilot = (state.get("state") or state).get("autopilot") or {}
        reason = autopilot_window(now(), autopilot, tasks, minutes)
        if reason is None or waited >= max_wait:
            stale = stale_queued_tasks(now(), tasks)
            if reason is None and stale and not waited:
                print(f"  ROUTING FAULT: queued task(s) {', '.join(stale)} have waited over {STALE_QUEUE_SECONDS // 3600} hours with no runner "
                      f"taking them (check that a lane polls their team); the case runs without waiting for them", flush=True)
            return reason
        sleep(step)
        waited += step
