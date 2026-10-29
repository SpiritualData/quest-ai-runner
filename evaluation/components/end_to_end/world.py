"""The small world an end-to-end case is sent into, and the real tool catalogue standing on it.

Nothing here decides an answer. ``World`` holds the quests of ``world.json`` (placeholder data, no real person), puts handlers
on the standard QAR tool registry (same names, descriptions and argument schemas as production) that read and change the
in-memory state and keep a log, and renders the record the way a host fills ``context_view``. ``FakeWeb`` is the web adapter
port answering from a fixture, so a web turn never leaves the process."""
import copy
import json
import os
from pathlib import Path

import common
from quest_ai_runner.core.adapters import Observation
from quest_ai_runner.core.tools import ToolResult
from quest_ai_runner.core.tools_quest_standard import NO_QUEST_HINT

SPEC = json.loads((Path(__file__).resolve().parent / "world.json").read_text(encoding="utf-8"))


class FakeWeb:
    """The ``WebResearch`` port over a fixture: every query gets the case's canned results, every page fetch fails."""

    def __init__(self, results):
        self.results, self.queries = results or [], []

    @property
    def backend_name(self):
        return "fixture"

    def describe(self):
        return "web search (fixture)"

    def search(self, queries, *, max_results=None, fresh=False):  # noqa: ARG002
        asked = [queries] if isinstance(queries, str) else list(queries)
        self.queries += asked
        lines = [f'WEB RESULTS for "{asked[0]}":']
        hits = []
        for i, hit in enumerate(self.results, 1):
            lines += [f"{i}. {hit['title']} ({hit['url']})", f"   {hit['snippet']}"]
            hits.append({**hit, "query": asked[0], "source": "fixture"})
        lines.append("(Cite as [title](url).)")
        return Observation(kind="query", rel_path=f"web_search:{asked[0][:80]}", text="\n".join(lines), hits=hits)

    def fetch(self, url, *, focus=None, fresh=False, full=False, part=None):  # noqa: ARG002
        return Observation(kind="error", rel_path=url, error="page fetch is not available in this evaluation")


class World:
    def __init__(self, pinned=None, web=None):
        self.quests = copy.deepcopy(SPEC["quests"])
        self.today = SPEC["today"]
        self.pinned = pinned
        self.web = FakeWeb(web)
        self.writes, self.reads = [], []
        self.registry = self.build_registry()

    # ---- the tool catalogue on this state
    def build_registry(self):
        from quest_ai_runner.core.tools import build_tool_registry
        registry = build_tool_registry(dict(os.environ, QUEST_BASE_URL=common.OFFLINE_QUEST_URL, QUEST_API_KEY="offline-eval"))
        for name in registry.names():
            spec = registry.get(name)
            spec.handler = self.handler(name, bool(getattr(spec, "mutates", False)), "quest_id" in (spec.properties() or {}))
        return registry

    def target(self, args):
        qid = args.get("quest_id") or self.pinned
        return qid if qid in self.quests else None

    def handler(self, name, mutates, takes_quest=False):
        def handle(args, ctx):  # noqa: ARG001
            args = dict(args)
            qid = self.target(args)
            if mutates and takes_quest and qid is None:
                # What the production handlers answer when no quest is named or pinned: nothing is written, the planner re-plans.
                return ToolResult(ok=False, text="No quest was named, so nothing was changed." + NO_QUEST_HINT)
            if mutates:
                self.writes.append({"tool": name, "args": {k: v for k, v in args.items() if k != "quest_id"}, "quest_id": qid})
                return f"ok: {name}"
            self.reads.append({"tool": name, "args": args})
            quest = self.quests.get(qid or "")
            if name == "list_quest_notes" and quest:
                return "\n".join(f"- {n}" for n in quest["notes"]) or "no notes"
            if name == "list_quest_goals" and quest:
                return "\n".join(f"{g['id']}: {g['title']} [{'done' if g['done'] else 'open'}] due {g['due']}" for g in quest["goals"]) or "no goals"
            if name == "find_quest":
                words = str(args.get("query") or args.get("text") or "").lower().split()
                rows = [f"{k}: {q['name']} - {q['outcome']}" for k, q in self.quests.items()
                        if not words or any(w in (q["name"] + " " + q["outcome"]).lower() for w in words)]
                return "\n".join(rows) or "no quest matches"
            return f"{name}: nothing to show in this evaluation"
        return handle

    # ---- the record a host passes as context_view
    def context_view(self):
        lines = [f"Today is {self.today}."]
        if self.pinned:
            q = self.quests[self.pinned]
            lines += [f"This conversation is about the quest {q['name']} (id {self.pinned}).", f"Outcome: {q['outcome']}",
                      f"Current state: {q['current_state']}"]
            lines += [f"Goal {g['id']}: {g['title']} ({'done' if g['done'] else 'open'}, due {g['due']})" for g in q["goals"]]
            lines += [f"Note: {n}" for n in q["notes"]]
        else:
            lines.append("No quest is pinned to this conversation. The person's quests (ask or look one up before acting on it):")
            lines += [f"- {k}: {q['name']}. Outcome: {q['outcome']}" for k, q in self.quests.items()]
        return "\n".join(lines)
