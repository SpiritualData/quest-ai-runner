"""What the production arms measure around one turn, as pure functions (offline-testable): the metric a case moves (read before and after
the turn as the acting account), the assistant messages of the turn in order, and the two-message standard the judge is told for a case that
acts. Nothing here calls a model or reads a word of the model's output to decide anything: the metric and the messages come from the
data read and the stream's own event types.

A case may carry, beside its text (all optional, from the dataset row or a rules file ``QAR_PROD_CASE_RULES_FILE`` keyed by case id):

* ``effect`` / ``state_check``: the structured forms the arm reads (``{"allow": [...]}`` and ``{"read": ..., "match": ..., "expect": n}``);
  a prose value (a description for people) allows and checks nothing.
* ``metric_probe``: ``{"name", "read": "<GET path>", "list_key", "match": {field: text}, "regex": "<one capture group>", "movable": bool,
  "computed": {"read", "list_key", "match", "regex"}}``. The value is the first number the regex captures from the first matching item (or
  from the whole answer when there is no ``match``). ``movable`` false (or an ``increment_kind`` that says the metric cannot move in one
  turn) records ``moved`` as ``not-movable-by-AI-in-one-turn``. ``computed`` is the same value worked out from readable data; when present
  the case passes only if the recorded value equals it.
"""
import json
import re
import time

NOT_MOVABLE = "not-movable-by-AI-in-one-turn"
MESSAGE_TYPES = ("interim", "milestone")     # the assistant's messages before the final one, as the stream types them


def structured(value):
    """The value when it is a machine-readable dict, else None (a prose description for people is not a rule)."""
    return value if isinstance(value, dict) else None


def is_action_case(case):
    """True for a case that asks the assistant to make progress (a milestone case, or one whose declared effect is not 'none')."""
    effect = case.get("effect")
    if case.get("group") == "milestone":
        return True
    return effect not in (None, "", {}) and not str(effect).strip().lower().startswith("none")


def items_of(payload, list_key=""):
    if isinstance(payload, dict) and list_key:
        return payload.get(list_key) or []
    if isinstance(payload, dict):
        return next((v for v in payload.values() if isinstance(v, list)), [payload])
    return payload if isinstance(payload, list) else []


def matches(item, match):
    return isinstance(item, dict) and all(str(want).lower() in str(item if key == "*" else item.get(key, "")).lower() for key, want in (match or {}).items())


def read_number(spec, client):
    """The number a read spec points at (``read``, ``list_key``, ``match``, ``regex``), or None when nothing matches."""
    payload = client._request("GET", str(spec["read"]))
    items = [i for i in items_of(payload, spec.get("list_key", "")) if matches(i, spec.get("match"))] if spec.get("match") else [payload]
    for item in items:
        found = re.search(spec.get("regex") or r"(-?\d[\d,]*\.?\d*)", json.dumps(item, default=str) if not isinstance(item, str) else item)
        if found:
            return float(found.group(1).replace(",", ""))
    return None


def read_metric(probe, client):
    """``{"name", "value", "computed"}`` now: the recorded value and, when the probe has a ``computed`` spec, the value worked out from data."""
    out = {"name": probe.get("name", ""), "value": read_number(probe, client)}
    if probe.get("computed"):
        out["computed"] = read_number(probe["computed"], client)
    return out


def metric_verdict(probe, before, after, increment_kind=""):
    """The metric block of a result: before, after, delta, moved (``yes`` / ``no`` / ``not-movable-by-AI-in-one-turn``) and, with a computed
    value, whether the recorded value equals it. Never inflated: a metric that did not change is ``no``, however good the step."""
    b, a = before.get("value"), after.get("value")
    delta = None if a is None or b is None else round(a - b, 6)
    movable = probe.get("movable", True) and "cannot move" not in str(increment_kind).lower() and "not movable" not in str(increment_kind).lower()
    moved = NOT_MOVABLE if not movable else ("yes" if delta else "no") if delta is not None else "unreadable"
    block = {"name": probe.get("name", ""), "before": b, "after": a, "delta": delta, "moved": moved}
    if probe.get("computed"):
        c = after.get("computed")
        block["computed"] = c
        block["matches_computed"] = c is not None and a is not None and abs(a - c) <= float(probe.get("tolerance", 0))
    return block


def messages_of(frames):
    """Every assistant message of the turn in order: the interim and progress messages the stream sent, then the final reply. Each is
    ``{"n", "t", "kind", "text"}`` (``t`` is seconds from the start of the turn, when the frame carries one). Status ticks are not messages."""
    out = []
    for frame in frames:
        kind = frame.get("type")
        text = str((frame.get("content") or frame.get("text") or "") if kind == "done" else frame.get("text") or "").strip()
        if kind not in MESSAGE_TYPES + ("done",) or not text:
            continue
        if out and out[-1]["text"] == text:
            continue
        out.append({"n": len(out) + 1, "t": frame.get("t"), "kind": "final" if kind == "done" else kind, "text": text})
    return out


TWO_MESSAGE_ITEM = ("The turn sends two useful messages in these roles (judge the roles, not the wording): message 1 states the CURRENT STATUS "
                    "(with the milestone's current metric value when known) and says what it is now doing to make progress; message 2, after "
                    "the work, reports what it did, the new state and the metric before and after. Both are grounded in what was read, with no "
                    "invented value.")
SINGLE_MESSAGE_ITEM = "A single message is correct for this request: it answers or replies directly and promises or does nothing more."


def sequence_note(case, messages, metric=None):
    """The text added to the judge's notes (and the rubric item) for this case: the messages in order, the metric block and the standard
    that applies (two roles for a case that acts, one message otherwise)."""
    lines = [f"MESSAGES IN ORDER: " + " | ".join(f"({m['n']}, {m['kind']}{', t=' + str(m['t']) + 's' if m.get('t') is not None else ''}) {m['text'][:700]}" for m in messages)]
    if metric:
        lines.append(f"METRIC {metric.get('name')}: before {metric.get('before')}, after {metric.get('after')}, delta {metric.get('delta')}, moved {metric.get('moved')}."
                     + (f" Computed from data: {metric.get('computed')}, equal: {metric.get('matches_computed')}." if "computed" in metric else "")
                     + " The metric is never claimed to have moved when it did not.")
    return " ".join(lines), (TWO_MESSAGE_ITEM if is_action_case(case) else SINGLE_MESSAGE_ITEM)


def load_rules(path):
    """``{case id: {"effect", "state_check", "metric_probe"}}`` from the rules file (JSON), or {} without one."""
    if not path:
        return {}
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def with_rules(case, rules):
    """The case with the structured rules of its id laid over a prose value (a structured value already on the case wins)."""
    extra = rules.get(case.get("id")) or {}
    merged = dict(case)
    for key in ("effect", "state_check", "metric_probe"):
        if key in extra and structured(case.get(key)) is None:
            merged[key] = extra[key]
    return merged


def stamp(started):
    return round(time.time() - started, 1)
