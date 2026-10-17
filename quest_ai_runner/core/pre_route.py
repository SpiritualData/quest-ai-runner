"""The pre-route: one small model call that may settle a CLEAR tool request before the planner runs.

The planner is the brain's whole routing job (route, tool choice, arguments, confirm or clarify,
reads). A cheap first call fronts it: it names the route and, for a plain create or update, the tool
calls with their arguments. The pre-route SETTLES the turn only when all of these hold:

* the route is ``tool`` and it named at least one call;
* it says it is sure;
* every call is on the caller's ``direct_tools`` list (never a destructive or outward tool);
* no call carries an argument that confirms something (``confirm_delete`` and the like).

Everything else (an answer, which needs the planner's reads; a deep run, which needs its brief; a
confirm or clarify; an unsure or guarded tool) falls through to the planner unchanged. So a failure
here, an unparsable reply or a missing provider is never worse than no pre-route.

Nothing here names an org, a database or a deployment: the caller supplies the tool names.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional

ROUTES = ("answer", "tool", "deep", "confirm", "clarify")

SYSTEM = (
    "You route ONE message sent to an assistant inside a goal-tracking app. Reply with ONLY JSON: "
    '{"route": "answer"|"tool"|"deep"|"confirm"|"clarify", "calls": [{"name": <tool>, "args": {...}}], '
    '"sure": true|false}.\n'
    "tool: the user asked to create, change or find something in their quest and listed tools do it. "
    "Fill calls with every tool needed and the arguments the user stated. Write only what was asked.\n"
    "answer: a question, advice, thanks, sharing news, or the user says not to save anything. "
    "Sharing a fact is NOT a request to record it.\n"
    "deep: needs a computer: code, scripts, files, deployments, long research reports.\n"
    "confirm: an irreversible, destructive or outward act (deleting, emailing, posting publicly) "
    "that a person must approve first.\n"
    "clarify: the request refers to something unnamed (it, that) so nothing can be done yet.\n"
    "sure: false when the message is ambiguous, risky, or could be read two ways.")


@dataclass
class PreRoute:
    route: str
    calls: List[Dict[str, Any]] = field(default_factory=list)
    sure: bool = False


def parse(text: Optional[str]) -> Optional[PreRoute]:
    """The model's reply as a PreRoute, or None when it is not the expected JSON."""
    match = re.search(r"\{.*\}", text or "", re.S)
    try:
        data = json.loads(match.group(0)) if match else None
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or data.get("route") not in ROUTES:
        return None
    calls = [{"name": c["name"], "args": c["args"] if isinstance(c.get("args"), dict) else {}}
             for c in (data.get("calls") or []) if isinstance(c, dict) and isinstance(c.get("name"), str)]
    return PreRoute(route=data["route"], calls=calls, sure=data.get("sure") is True)


def confirms_something(args: Dict[str, Any]) -> bool:
    return any(key.startswith("confirm") and bool(value) for key, value in args.items())


class PreRouter:
    """``route(message, tool_names)`` asks the model once. ``direct_tools`` are the tools a sure
    pre-route may call without the planner."""

    def __init__(self, provider: Any, model: str, direct_tools: Iterable[str] = ()) -> None:
        self.provider = provider
        self.model = model
        self.direct_tools = frozenset(direct_tools)

    def route(self, message: str, tool_names: Iterable[str]) -> Optional[PreRoute]:
        prompt = f"Tools: {', '.join(tool_names)}\nMessage: {message}"
        try:
            text = self.provider.answer([{"role": "user", "content": prompt}], model=self.model, system=SYSTEM)
        except Exception:  # noqa: BLE001  a failed pre-route must cost nothing: the planner decides
            return None
        return parse(text)

    def settles(self, pre: Optional[PreRoute]) -> bool:
        """True when the pre-route's tool calls may run without the planner."""
        return bool(pre and pre.route == "tool" and pre.sure and pre.calls
                    and all(c["name"] in self.direct_tools and not confirms_something(c["args"])
                            for c in pre.calls))
