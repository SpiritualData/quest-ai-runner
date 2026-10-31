"""Runs one end-to-end case through the real orchestrator and folds the outcome into the record ``interface.py`` names.

The arms differ only in the model at the boundary and build their own orchestrator (a planner that replays the case's ideal
decisions, or the way production builds it on the claude CLI backend); ``run_terminal`` runs the real ``send`` command. A fresh orchestrator, scratch corpus and world per case, so no case sees
another's turns."""
from assist_a_terminal_session import arms as terminal_arms
from end_to_end.world import World
from hubofhubs.ports.deep import DeepResult
from scripted_brain import ScriptedProvider

PRIORITY = ("confirm", "clarify", "deep", "queue", "act", "web", "answer")


class IdealPlanner(ScriptedProvider):
    """The scripted planner of a case: its ideal decisions in order, then its ideal reply."""

    def __init__(self, decisions, reply):
        super().__init__(decisions)
        self.reply = reply

    def answer(self, messages, *, model, system=None):  # noqa: ARG002
        return self.reply


def handed_off(result):
    """True when the planner asked for deep work in an answer turn (answer + deferred hand-off) that nothing executed: the
    execution record holds a fact that neither succeeded nor failed (no deep runner stands in the offline world; a deployment's
    runner would queue it). A structured fact of the run, never a reading of the reply."""
    facts = getattr(getattr(result, "execution_record", None), "facts", None) or []
    return any(not f.succeeded and not f.failed for f in facts)


def fold(case, world, result):
    """The record of a finished turn: reply, writes, reads, web queries and the one route it took."""
    kind = getattr(result, "kind", "answer")
    exit_reason = getattr(result, "exit_reason", "") or ""
    tags = set()
    if kind == "confirm" or exit_reason in ("confirm", "overseer_escalated_human", "parked"):
        tags.add("confirm")
    if exit_reason == "clarify":
        tags.add("clarify")
    if kind == "deep" or handed_off(result):
        tags.add("deep")
    if any(w["tool"] == "create_task" for w in world.writes):
        tags.add("queue")
    if any(w["tool"] != "create_task" for w in world.writes):
        tags.add("act")
    if world.web.queries:
        tags.add("web")
    text = getattr(result, "text", None) or getattr(result, "question", None) or ""
    route = next((t for t in PRIORITY if t in tags), "answer")
    return dict(reply=str(text), writes=list(world.writes), reads=[r["tool"] for r in world.reads], web=list(world.web.queries),
                route=route, exit_reason=exit_reason, kind=kind)


def new_world(case):
    spec = case.get("world") or {}
    return World(pinned=spec.get("pinned"), web=case.get("web"), docs=spec.get("docs"), docs_only=bool(spec.get("docs_only")),
                 science=bool(spec.get("science")), bulk=spec.get("bulk"), history=spec.get("history"))


def run_with(case, world, orchestrator):
    """Send the case's message through ``orchestrator`` (built by the arm over ``world``) and fold the outcome."""
    result = orchestrator.run(case["message"], quest_id=world.pinned, context_view=world.context_view())
    return fold(case, world, result)


def run_terminal(case, provider_factory):
    """The real ``send`` command; ``provider_factory`` is None for the no-model floor."""
    out = terminal_arms.run_ack({"request": case["message"], "create_task": "ok"}, provider_factory)
    return dict(reply=out["stdout"], writes=[], reads=[], web=[], route="terminal", terminal=out)


class BulkRunner:
    """A deep runner standing on a ``world.bulk`` collection, working the way a careful worker would: count first, then page to the
    end (or to the case's ``work_budget_pages`` spent across the whole turn), and DECLARE what it covered as ``DeepResult.coverage``
    from its own bookkeeping. A later attempt resumes at the point the earlier one reached. ``capped_only=True`` is the failure it
    stands in for: one page, no count, no declaration."""

    def __init__(self, world, capped_only=False):
        self.world, self.capped_only = world, capped_only
        self.counted, self.total, self.next_skip, self.pages_used, self.rows = False, None, 0, 0, []
        spec = world.bulk.spec
        self.budget = spec.get("work_budget_pages")
        meaning = spec.get("notes_paraphrase_rate") is not None
        self.method = ("grouped the notes by meaning with a model step, not by matching words" if meaning
                       else "scored each entry's growth change against its watering and light settings, then compared the best and worst weeks")
        self.method_kind = "meaning" if meaning else "scored"
        self.classification = "model" if meaning else None

    def run_goal(self, *, goal, brief, model=None, max_turns=None, context_preamble=None):  # noqa: ARG002
        bulk = self.world.bulk
        if self.capped_only:
            self.rows = bulk.find(0)
            self.method, self.method_kind = "counted keywords in the first page", "keyword"
            return DeepResult(met=True, output=self.summary())
        if not self.counted:
            self.total, self.counted = bulk.count(), True
            self.world.reads.append({"tool": "count", "args": {}})
        while self.next_skip < self.total and (self.budget is None or self.pages_used < self.budget):
            page = bulk.find(self.next_skip)
            self.world.reads.append({"tool": "find", "args": {"skip": self.next_skip}})
            self.rows += page
            self.next_skip += len(page)
            self.pages_used += 1
        processed = len(self.rows)
        coverage = {"processed": processed, "total": self.total, "unit": "entries", "complete": processed >= self.total,
                    "capped": processed < self.total, "remaining": self.total - processed, "next_skip": self.next_skip}
        return DeepResult(met=True, output=self.summary(), coverage=coverage)

    def summary(self):
        by_month = {}
        for row in self.rows:
            by_month.setdefault(row["entry_date"][:7], []).append(row["growth_cm"])
        best = max(by_month, key=lambda m: sum(by_month[m]) / len(by_month[m])) if by_month else "none"
        return f"Method: {self.method}.\nHighest mean growth month over the {len(self.rows)} entries processed: {best}."


class BulkProvider(IdealPlanner):
    """The scripted planner of a bulk case, also answering the pre-flight (always sufficient) and counting its calls."""

    def __init__(self, decisions, reply):
        super().__init__(decisions, reply)
        self.preflight_calls = 0

    def plan(self, prompt, *, model, tool_schema):
        if tool_schema.get("name") == "plan_sufficiency":
            self.preflight_calls += 1
            return {"sufficient": True}
        return super().plan(prompt, model=model, tool_schema=tool_schema)


def run_bulk(case, meter, capped_only=False):
    """One bulk-analysis case through the real orchestrator with the done-evidence feature on, a ``BulkRunner`` as the deep worker
    and a sink that keeps every event. Folds the usual record plus the facts the bulk expectations read."""
    from quest_ai_runner.core.adapters_sinks import StreamSink
    from quest_ai_runner.core.orchestrator import OrchestratorConfig
    import scripted_brain
    world = new_world(case)
    spec = world.bulk.spec
    script = case["ideal"]
    provider = meter.wrap(BulkProvider([dict(d) for d in script["decisions"]], script["reply"]))
    runner = BulkRunner(world, capped_only=capped_only)
    limits = (f"The data helper {spec['helper']} returns at most {world.bulk.page_cap} rows per call from a skip offset; "
              f"{spec['count_helper']} gives the total. Generated code cannot call a model.")
    brain = scripted_brain.build_brain(provider, tools=world.registry, web=world.web, deep_runner=runner,
                                       config=OrchestratorConfig(done_evidence_enabled=True, consumer_limits=limits))
    events = []
    result = brain.run(case["message"], quest_id=world.pinned, context_view=world.context_view(),
                       transcript=world.transcript(), sink=StreamSink(events.append))
    got = fold(case, world, result)
    if not got["reply"].strip():
        # A deep turn offline ends without a synthesised answer: the reply is what the scripted model would say, then what the
        # worker's own result (method, coverage and how to continue, all written by code) adds under it.
        got["reply"] = script["reply"] + "\n\n" + "\n".join(getattr(d, "output", "") or "" for d in (getattr(result, "deep_results", None) or []))
    covered, pages = world.bulk.covered()
    got.update(deep_output="\n".join(getattr(d, "output", "") or "" for d in (getattr(result, "deep_results", None) or [])),
               goal=(script["decisions"][0].get("goal") or ""), interim=[e for e in events if e.get("type") == "interim"],
               preflight_calls=provider.preflight_calls,
               bulk=dict(covered=covered, pages=pages, method_kind=runner.method_kind, classification=runner.classification,
                         truth_month=world.bulk.truth_month()))
    return got
