"""In-memory quest behind the real QAR tool catalog. No network, no Quest server, no deep run."""
import copy
import os

import common  # noqa: F401

INITIAL = {
    "current_state": "Runs 10K in 58 minutes, three runs a week.",
    "goals": [],
    "outcomes": [],
    "habits": [],
    "experiments": [{"id": "rq_sleep", "question": "Does stretching before bed improve my sleep?",
                     "status": "experimenting", "conclusion": "", "hypothesis": ""}],
}


class FakeQuest:
    quest_id = "quest_eval"

    def __init__(self):
        self.state = copy.deepcopy(INITIAL)
        self.calls = []  # (tool, args) for every handler invoked, in order
        self.registry = self._build_registry()

    def reset(self):
        self.state = copy.deepcopy(INITIAL)
        self.calls = []

    def _build_registry(self):
        from quest_ai_runner.core.tools import build_tool_registry
        env = dict(os.environ, QUEST_BASE_URL=common.OFFLINE_QUEST_URL, QUEST_API_KEY="offline-eval")
        registry = build_tool_registry(env)
        for name in registry.names():
            spec = registry.get(name)
            spec.handler = self._handler(name)
        return registry

    def _handler(self, name):
        def handle(args, ctx):  # noqa: ARG001
            args = {k: v for k, v in dict(args).items() if k != "quest_id"}
            self.calls.append((name, args))
            s = self.state
            if name == "create_quest_goal":
                s["goals"].append(args)
            elif name == "update_quest_current_state":
                s["current_state"] = args.get("value", "")
            elif name == "add_quest_measurable_outcome":
                s["outcomes"].append(args)
            elif name == "create_quest_habit":
                s["habits"].append(args)
            elif name == "create_quest_experiment":
                s["experiments"].append(dict(args, id=f"rq_{len(s['experiments']) + 1}", status="open"))
            elif name == "update_quest_experiment":
                for e in s["experiments"]:
                    if e["id"] == args.get("research_question_id"):
                        e.update({k: v for k, v in args.items()
                                  if k not in ("research_question_id", "user_asked_for_this_field")})
            elif name == "find_quest_experiments":
                q = (args.get("query") or "").lower()
                rows = [e for e in s["experiments"] if not q or any(w in e["question"].lower() for w in q.split())]
                return "\n".join(f"{e['id']}: {e['question']} [{e['status']}] {e.get('conclusion', '')}"
                                 for e in rows) or "no experiments"
            else:
                return f"{name}: not available in this offline world"
            return f"ok: {name}"
        return handle

    def mutations(self):
        """Tool calls that wrote (everything except the read-only finder)."""
        return [(n, a) for n, a in self.calls if n != "find_quest_experiments"]


WORLD = FakeQuest()
