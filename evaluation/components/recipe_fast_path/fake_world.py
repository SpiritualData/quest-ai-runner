"""Five fake quest tools over an in-memory call log, built from the REAL ``ToolRegistry`` / ``ToolSpec``
types so a planner sees real tool descriptions and schemas. No Quest server, no network, no deep run."""
import common  # noqa: F401
from quest_ai_runner.core.tools import ToolRegistry, ToolResult, ToolSpec

TEACH_TOOLS = {"complete_habit": "mark my meditation habit done",
               "add_note": "add a note saying call the dentist on Friday",
               "log_time": "log 20 minutes on my running habit",
               "add_goal": "add a weekly goal called finish the quarterly report",
               "habit_streak": "what is my streak on the meditation habit"}


class RecipeWorld:
    def __init__(self):
        self.calls = []                     # (tool, args) in order, every invocation
        self.registry = self._registry()

    def reset(self):
        self.calls.clear()

    def _registry(self):
        def spec(name, desc, when, params, mutates, fn):
            def handler(args, ctx):  # noqa: ARG001
                self.calls.append((name, dict(args)))
                return ToolResult(ok=True, text=fn(args))
            props = {k: {"type": t} for k, t in params.items()}
            return ToolSpec(name=name, description=desc, handler=handler, when_to_use=when,
                            when_not_to_use="questions about the thing rather than doing it",
                            mutates=mutates, parameters={"type": "object", "required": list(params),
                                                         "properties": props})
        return ToolRegistry([
            spec("complete_habit", "Mark one habit as done for today.",
                 "The person says they did a habit and wants it checked off.",
                 {"habit": "string"}, True, lambda a: f"Marked the {a.get('habit')} habit done for today."),
            spec("add_note", "Save a short note on the quest.",
                 "The person asks to add or save a note.", {"text": "string"}, True,
                 lambda a: f"Saved note: {a.get('text')}"),
            spec("log_time", "Log minutes spent on a habit today.",
                 "The person says how long they spent on a habit.",
                 {"habit": "string", "minutes": "integer"}, True,
                 lambda a: f"Logged {a.get('minutes')} minutes on {a.get('habit')}."),
            spec("add_goal", "Create a goal for a period.",
                 "The person asks to add or create a goal.",
                 {"title": "string", "period": "string"}, True,
                 lambda a: f"Created {a.get('period')} goal: {a.get('title')}."),
            spec("habit_streak", "Read the current streak of a habit.",
                 "The person asks about the streak or status of a habit.",
                 {"habit": "string"}, False, lambda a: f"Your {a.get('habit')} streak is 6 days."),
        ])
