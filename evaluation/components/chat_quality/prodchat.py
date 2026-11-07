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
import hashlib
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
               ("tasks", r"/api/assistant-tasks/([\w-]+)", "DELETE"), ("notes", r"/api/quests/{quest}/notes/([\w-]+)", "DELETE"))


class ProdClient:
    """Bearer-key HTTP client for production. GETs are free; the only writes it will make are the three below, on the conversation it created."""

    def __init__(self, base, key, quest_id=None, team_id=None, timezone=""):
        self.base = check_base_url(base)
        self.quest_id = quest_id or target("QAR_PROD_QUEST_ID")
        self.team_id = team_id or target("QAR_PROD_TEAM_ID")
        self.key = key
        self.timezone = timezone
        self.own_conversations = set()
        self.removable = set()           # ("decisions"|"tasks"|"workflows"|"notes", id) this run created and may undo; nothing else is ever touched

    def _headers(self, accept="application/json"):
        return {"Authorization": f"Bearer {self.key}", "Content-Type": "application/json", "Accept": accept}

    def _request(self, method, path, body=None, params=None, timeout=60):
        """JSON call. Anything but a GET must be one of the three allowed writes (``allow_write``)."""
        if method != "GET":
            self.allow_write(method, path)
        url = self.base + path + ("?" + urllib.parse.urlencode(params) if params else "")
        req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None, method=method, headers=self._headers())
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode()
                return json.loads(raw) if raw.strip() else None
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"{method} {path}: {e.code} {e.read().decode()[:300]}") from None

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
            return self._request("GET", "/api/teams/whoami") or {}
        except Exception as e:  # noqa: BLE001
            return {"error": f"{type(e).__name__}: {e}"}

    def create_conversation(self):
        """A fresh chat pinned to the quest the way the Quest app opens a quest chat: ``quest_ids`` plus the quest's team."""
        body = self._request("POST", "/api/quest-ai/conversations", {"quest_ids": [self.quest_id], "team_id": self.team_id})
        conv = (body or {}).get("conversation_id") or (body or {}).get("id") or ((body or {}).get("data") or {}).get("id")
        if not conv:
            raise RuntimeError(f"create conversation returned no id: {str(body)[:200]}")
        self.own_conversations.add(conv)
        return conv

    def stop(self, conv):
        try:
            self._request("POST", f"/api/quest-ai/conversations/{conv}/stop", {})
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
        req = urllib.request.Request(self.base + path, data=json.dumps(payload).encode(), method="POST", headers=self._headers("text/event-stream"))
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
    """``{"model_calls", "tokens_in", "tokens_out"}`` from the stream's ``tokens`` frames (the last cumulative count wins)."""
    tokens = [e.get("data") or {} for e in events if event_name(e) == "tokens"]
    last = tokens[-1] if tokens else {}
    return {"model_calls": sum(model_calls_in(e) for e in events), "tokens_in": int(last.get("tokens_in") or 0), "tokens_out": int(last.get("tokens_out") or 0)}


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


def fingerprint_from(reads):
    """The fingerprint from raw reads ``{name: payload}`` (a pure function): ``{category: {id: {"digest", "label", ...}}}``."""
    fp = {c: {} for c in CATEGORIES}
    for note in reads.get("notes") or []:
        nid = note.get("note_id") or note.get("id")
        fp["notes"][nid] = {"digest": digest(note.get("text"), note.get("updated_at")), "label": str(note.get("text") or "")[:80], "author": note.get("author_name"),
                                  "author_id": note.get("author_id"), "created_at": note.get("created_at")}
    for goal in reads.get("goals") or []:
        fp["goals"][goal.get("id")] = {"digest": digest(goal.get("name"), goal.get("completed"), goal.get("criteria"), goal.get("description"), goal.get("deadline"), goal.get("scheduled_date")),
                                       "label": str(goal.get("name") or "")[:80]}
    for wf in reads.get("workflows") or []:
        fp["workflows"][wf.get("workflowId")] = {"digest": digest(wf.get("title"), wf.get("status"), wf.get("updatedAt"), wf.get("steps")), "label": str(wf.get("title") or "")[:80], "created_at": wf.get("createdAt")}
    for rec in reads.get("spec_records") or []:
        fp["spec_records"][rec.get("recordId")] = {"digest": digest(rec.get("revision"), rec.get("updatedAt"), rec.get("fields")), "label": f"{rec.get('type')}"}
    for task in reads.get("tasks") or []:
        fp["tasks"][task.get("task_id") or task.get("id")] = {"digest": "", "label": str(task.get("title") or task.get("text") or "")[:80], "conv_id": task.get("conv_id"), "created_by": task.get("created_by"),
                                                               "source": task.get("source"), "created_at": task.get("created_at")}
    for dec in reads.get("decisions") or []:
        fp["decisions"][dec.get("decision_id")] = {"digest": "", "label": str(dec.get("summary") or "")[:80], "conv_id": dec.get("conv_id"), "kind": dec.get("kind"), "status": dec.get("status")}
    for conv in reads.get("conversations") or []:
        fp["conversations"][conv.get("id")] = {"digest": "", "label": str(conv.get("title") or "")[:80]}
    return fp


def read_fingerprint(client, own_conv=None):
    """Read the quest as the account sees it right now (GETs only)."""
    def read(path, key=None, params=None):
        try:
            body = client._request("GET", path, params=params)
        except Exception as e:  # noqa: BLE001 -- an unreadable category must fail loudly, never look like "unchanged"
            raise RuntimeError(f"fingerprint read failed: {path}: {e}") from None
        if key and isinstance(body, dict):
            return body.get(key) or []
        return body if isinstance(body, list) else []
    quest_id, team_id = client.quest_id, client.team_id
    goals_body = client._request("GET", f"/api/teams/{team_id}/quests/{quest_id}/goals") or {}
    reads = {
        "notes": read(f"/api/quests/{quest_id}/notes"),
        "goals": [g for group in goals_body.get("period_groups") or [] for g in group.get("goals") or []],
        "workflows": read("/api/workflows", "items") + read(f"/api/quests/{quest_id}/workflows", "items"),
        "spec_records": read(f"/api/intents/specs/quest/{quest_id}", "records"),
        "tasks": read("/api/assistant-tasks", "tasks", {"quest_id": quest_id, "limit": 1000}),
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
                changes.append(dict(meta, category=cat, change="changed", id=item_id))
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


def unexpected_writes(changes, effect=None, zero_writes=False):
    """The changes the case's allowed writes do not explain. A zero-writes case explains none; otherwise a change is explained when its category is
    one the ``effect`` allow-list names. Each is returned as a readable ``UNEXPECTED WRITE`` line."""
    allowed = set() if zero_writes else allowed_categories(effect)
    out = []
    for c in changes:
        if c["category"] in allowed:
            continue
        extra = ", ".join(f"{k}={c[k]}" for k in ("author", "conv_id", "source", "created_at", "kind") if c.get(k))
        out.append(f"UNEXPECTED WRITE: {c['category']} {c['change']} {c['id']} {c.get('label', '')!r}" + (f" ({extra})" if extra else ""))
    return out


# ---------------------------------------------------------------------------------------------------------------- cleanup: nothing a run creates needs a human to undo

def describe_undo(effect, zero_writes):
    """The line printed before a case runs: what it may create and how each kind is undone."""
    kept = sorted(allowed_categories(effect)) if not zero_writes else []
    undo = "; ".join(f"{cat}: {verb} {pattern.split('/api/')[1].replace('{quest}', '<quest>').replace('([\\w-]+)', '<id>')}" for cat, pattern, verb in UNDO_ROUTES)
    return (f"intended results kept (idempotent): {', '.join(kept) or 'none (a zero-writes case)'}; anything else this run creates is taken back as the AI account "
            f"({undo}); a change that cannot be taken back fails the case")


def plan_cleanup(changes, own_conv, started_iso, ai_user_id, effect=None, zero_writes=False):
    """Split the fingerprint changes into ``remove`` (created by THIS run, not the case's intended result, with a route that takes it back), ``keep`` (the intended,
    allowed result) and ``left`` (cannot be taken back or cannot be shown to be this run's: a changed or removed item, a goal or spec record, a note or workflow that
    is not provably this run's). Attribution: a decision or task by this run's conversation id; a note by the AI account created since the turn began; a workflow created
    since the turn began. An item that existed before, or that another actor created, is never in ``remove``."""
    allowed = set() if zero_writes else allowed_categories(effect)
    plan = {"remove": [], "keep": [], "left": []}
    for c in changes:
        cat, item_id = c["category"], c["id"]
        if c["change"] != "added":
            plan["left"].append(dict(c, why=f"{c['change']} (cannot be undone from here)"))
        elif cat in allowed:
            plan["keep"].append(c)
        elif cat in ("decisions", "tasks") and own_conv and c.get("conv_id") == own_conv:
            plan["remove"].append(c)
        elif cat == "notes" and c.get("author_id") == ai_user_id and str(c.get("created_at") or "") >= started_iso:
            plan["remove"].append(c)
        elif cat == "workflows" and str(c.get("created_at") or "") >= started_iso:
            plan["remove"].append(c)
        else:
            plan["left"].append(dict(c, why="not provably created by this run, or no route takes it back"))
    return plan


def run_cleanup(client, plan):
    """Take back what the plan says to remove, as the AI account. Returns ``{"withdrawn": [ids], "deleted": [ids], "left": [text], "kept": [ids]}``; an item whose
    removal fails is ``left``."""
    out = {"withdrawn": [], "deleted": [], "left": [f"{c['category']} {c['id']} ({c['why']})" for c in plan["left"]], "kept": [c["id"] for c in plan["keep"]]}
    for c in plan["remove"]:
        cat, item_id = c["category"], c["id"]
        client.removable.add((cat, item_id))
        path, method = {"decisions": (f"/api/teams/decisions/{item_id}/withdraw", "POST"), "workflows": (f"/api/workflows/{item_id}", "DELETE"),
                        "tasks": (f"/api/assistant-tasks/{item_id}", "DELETE"), "notes": (f"/api/quests/{client.quest_id}/notes/{item_id}", "DELETE")}[cat]
        try:
            client._request(method, path, {} if method == "POST" else None)
            (out["withdrawn"] if cat == "decisions" else out["deleted"]).append(item_id)
        except Exception as e:  # noqa: BLE001
            out["left"].append(f"{cat} {item_id} (removal failed: {str(e)[:120]})")
    return out


def cleanup_line(result):
    return f"cleanup: withdrawn {result['withdrawn'] or 'none'}, deleted {result['deleted'] or 'none'}, left {result['left'] or 'none'}"
