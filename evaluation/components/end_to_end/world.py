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
from quest_ai_runner.core.adapters_values import Observation
from quest_ai_runner.core.tools import ToolResult
from quest_ai_runner.core.tools_quest_standard import NO_QUEST_HINT

SPEC = json.loads((Path(__file__).resolve().parent / "world.json").read_text(encoding="utf-8"))
SCIENCE_WORKS = json.loads((Path(__file__).resolve().parent / "science_works.json").read_text(encoding="utf-8"))["works"]


def openalex_record(work):
    """One invented work as an OpenAlex works record (the shape the hub's OpenAlex arm reads), abstract as an inverted index."""
    inverted = {}
    for position, word in enumerate(work["abstract"].split()):
        inverted.setdefault(word, []).append(position)
    return {"id": f"https://openalex.org/{work['id']}", "doi": f"https://doi.org/{work['doi']}" if work["doi"] else None,
            "display_name": work["title"], "publication_year": work["year"], "cited_by_count": work["cited_by"],
            "authorships": [{"author": {"display_name": a}} for a in work["authors"]],
            "primary_location": {"landing_page_url": work["url"], "source": {"display_name": work["venue"]}},
            "open_access": {"is_oa": bool(work["oa"]), "oa_status": work["oa"]}, "abstract_inverted_index": inverted}


class FakeOpenAlex:
    """The OpenAlex HTTP call the hub arm makes, answered from the invented works: every search returns all of them. Keeps the
    requests it was sent."""

    class Reply:
        status_code, headers = 200, {}

        def __init__(self, body):
            self.body = body

        def json(self):
            return self.body

    def __init__(self, works):
        self.works, self.calls = works, []

    def __call__(self, url, *, params, headers, timeout):  # noqa: ARG002
        self.calls.append({"url": url, "params": dict(params)})
        records = [openalex_record(w) for w in self.works]
        return self.Reply({"meta": {"count": len(records), "cost_usd": 0.001}, "results": records})


class FakeWeb:
    """The ``WebResearch`` port over a fixture: every query gets the case's canned results, every page fetch fails."""

    def __init__(self, results):
        self.results, self.queries, self.trace = results or [], [], []

    @property
    def backend_name(self):
        return "fixture"

    def describe(self):
        return "web search (fixture)"

    def search(self, queries, *, max_results=None, fresh=False):  # noqa: ARG002
        asked = [queries] if isinstance(queries, str) else list(queries)
        self.queries += asked
        first = self.results[0] if self.results else {}
        self.trace.append({"kind": "web", "query": asked[0], "out": f"{first.get('title', '')}: {first.get('snippet', '')}"[:300]})
        lines = [f'WEB RESULTS for "{asked[0]}":']
        hits = []
        for i, hit in enumerate(self.results, 1):
            lines += [f"{i}. {hit['title']} ({hit['url']})", f"   {hit['snippet']}"]
            hits.append({**hit, "query": asked[0], "source": "fixture"})
        lines.append("(Cite as [title](url).)")
        return Observation(kind="query", rel_path=f"web_search:{asked[0][:80]}", text="\n".join(lines), hits=hits)

    def fetch(self, url, *, focus=None, fresh=False, full=False, part=None):  # noqa: ARG002
        return Observation(kind="error", rel_path=url, error="page fetch is not available in this evaluation")


DOCS_MODE_RULES = (
    "You are in documentation mode: the person asks about the product itself. Answer only from the documentation excerpts "
    "below and name the page you took each fact from. If the excerpts do not cover what was asked, say you are not sure and "
    "that the support team can confirm; never invent a feature, a plan name or a price. You cannot read or change the "
    "person's own quests here. Do not use em dashes.")


class World:
    """``docs`` names the documentation excerpts of ``world.json`` shown to the planner; ``docs_only`` makes the view the
    documentation-only chat a free plan gets (no quests, only the excerpts). ``trace`` is the ordered log of what the
    tools and the web did, kept so a transcript can show the steps; nothing reads it for scoring."""

    def __init__(self, pinned=None, web=None, docs=None, docs_only=False, science=False):
        self.quests = copy.deepcopy(SPEC["quests"])
        self.today = SPEC["today"]
        self.pinned = pinned
        self.docs = [dict(SPEC["docs"][d], id=d) for d in docs or []]
        self.docs_only = docs_only
        self.trace = []
        self.web = FakeWeb(web)
        self.web.trace = self.trace
        self.writes, self.reads = [], []
        self.science = science
        self.openalex = FakeOpenAlex(SCIENCE_WORKS)
        self.registry = self.build_registry()

    # ---- the tool catalogue on this state
    def build_registry(self):
        from quest_ai_runner.core.tools import build_tool_registry
        # No literature key unless the case configures the tool: a key in the machine's own environment must not reach the world.
        env = {k: v for k, v in os.environ.items() if k != "OPENALEX_API_KEY"}
        registry = build_tool_registry(dict(env, QUEST_BASE_URL=common.OFFLINE_QUEST_URL, QUEST_API_KEY="offline-eval"))
        for name in registry.names():
            spec = registry.get(name)
            spec.handler = self.handler(name, bool(getattr(spec, "mutates", False)), "quest_id" in (spec.properties() or {}))
        if self.science:
            # The real tool over the hub's OpenAlex arm, with the HTTP call replaced by the fake: only the network is stubbed.
            from quest_ai_runner.core.tools_science import science_literature_spec
            spec = science_literature_spec({"OPENALEX_API_KEY": "offline-eval"}, transport=self.openalex)
            real = spec.handler

            def searched(args, ctx):
                result = real(args, ctx)
                self.reads.append({"tool": spec.name, "args": dict(args)})
                self.trace.append({"kind": "read", "tool": spec.name, "args": dict(args), "out": str(getattr(result, "text", ""))[:300]})
                return result
            spec.handler = searched
            registry.register(spec)
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
                self.trace.append({"kind": "write", "tool": name, "args": args, "quest_id": qid})
                return f"ok: {name}"
            self.reads.append({"tool": name, "args": args})
            text = self.read(name, args, qid)
            self.trace.append({"kind": "read", "tool": name, "args": args, "quest_id": qid, "out": text[:300]})
            return text
        return handle

    def read(self, name, args, qid):
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

    # ---- the record a host passes as context_view
    def context_view(self):
        lines = [f"Today is {self.today}."]
        if self.docs_only:
            lines.append(DOCS_MODE_RULES)
            for d in self.docs:
                lines += ["", f"Documentation excerpt, page {d['source']} ({d['title']}):", d["text"]]
            return "\n".join(lines)
        if self.pinned:
            q = self.quests[self.pinned]
            lines += [f"This conversation is about the quest {q['name']} (id {self.pinned}).", f"Outcome: {q['outcome']}",
                      f"Current state: {q['current_state']}"]
            lines += [f"Goal {g['id']}: {g['title']} ({'done' if g['done'] else 'open'}, due {g['due']})" for g in q["goals"]]
            lines += [f"Note: {n}" for n in q["notes"]]
        else:
            lines.append("No quest is pinned to this conversation. The person's quests (ask or look one up before acting on it):")
            lines += [f"- {k}: {q['name']}. Outcome: {q['outcome']}" for k, q in self.quests.items()]
        for d in self.docs:
            lines += ["", f"Documentation excerpt, page {d['source']} ({d['title']}):", d["text"]]
        return "\n".join(lines)
