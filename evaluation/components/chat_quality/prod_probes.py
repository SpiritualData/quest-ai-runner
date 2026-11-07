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


def message_standard(case):
    """``"single"`` or ``"two-role"``, from the case's own data and never from the reply. A case that writes nothing (``zero_writes``) or to which ESOI
    does not apply (``esoi.esoi_applies`` false) is a reply-only case: one message is complete. A case whose effect allows a write, that names an
    ``increment_kind`` or that is a milestone case acts (unless the state read before the run showed the thing already there: ``already_there``, one complete message), so it owes a status message first and a report after."""
    if case.get("zero_writes") or case.get("already_there") or (isinstance(case.get("esoi"), dict) and case["esoi"].get("esoi_applies") is False):
        return "single"
    effect = case.get("effect")
    allows = bool(effect.get("allow")) if isinstance(effect, dict) else effect not in (None, "") and not str(effect).strip().lower().startswith("none")
    kind = str(case.get("increment_kind") or "").strip()
    if allows or case.get("group") == "milestone" or (kind and not kind.lower().startswith("none")):
        return "two-role"
    return "single"


def is_action_case(case):
    """True when the case owes the two-role standard (see ``message_standard``)."""
    return message_standard(case) == "two-role"


def items_of(payload, list_key=""):
    """The list a read points at. ``list_key`` may be a dotted path whose lists are flattened ("period_groups.goals")."""
    if isinstance(payload, dict) and list_key:
        nodes = [payload]
        for part in str(list_key).split("."):
            following = []
            for node in nodes:
                value = node.get(part) if isinstance(node, dict) else None
                following.extend(value if isinstance(value, list) else [] if value is None else [value])
            nodes = following
        return nodes
    if isinstance(payload, dict):
        return next((v for v in payload.values() if isinstance(v, list)), [payload])
    return payload if isinstance(payload, list) else []


def matches(item, match, min_len=None):
    """True when every ``match`` text is in the item's field (``*`` is the whole item; ``=field`` must equal exactly; ``^field`` must start with the text)
    and every ``min_len`` field is long enough. A field may be a dotted path into nested objects ("timing.repeat.freq"), and a text may be a list, all of
    whose entries must be in the field (case-insensitive)."""
    if not isinstance(item, dict):
        return False
    for key, want in (match or {}).items():
        name = key.lstrip("=^")
        value = item if key == "*" else pick_of(item, name) if "." in name else item.get(name, "")
        have = json.dumps(value, default=str) if key == "*" else str(value if value is not None else "")
        for text in (want if isinstance(want, list) else [want]):
            text = str(text).strip().lower()
            if key.startswith("="):
                ok = have.strip().lower() == text
            elif key.startswith("^"):
                ok = have.strip().lower().startswith(text)
            else:
                ok = text in have.lower()
            if not ok:
                return False
    return all(len(str(item.get(field) or "")) >= int(need) for field, need in (min_len or {}).items())


def expand(value, today=None):
    """Placeholders in a rule: ``{monday}`` (ISO date of this week's Monday), ``{week}`` (YYYY_W##), ``{today}``."""
    import datetime
    day = today or datetime.date.today()
    monday = day - datetime.timedelta(days=day.weekday())
    iso = day.isocalendar()
    fill = {"{monday}": monday.isoformat(), "{week}": f"{iso[0]}_W{iso[1]:02d}", "{today}": day.isoformat()}
    if isinstance(value, str):
        for mark, text in fill.items():
            value = value.replace(mark, text)
        return value
    if isinstance(value, dict):
        return {k: expand(v, day) for k, v in value.items()}
    if isinstance(value, list):
        return [expand(v, day) for v in value]
    return value


def pick_of(item, path):
    """The value at a dotted path inside an item ("fields.record"), else the item itself."""
    for part in str(path or "").split("."):
        if part:
            item = item.get(part) if isinstance(item, dict) else None
    return item


def read_number(spec, client):
    """The number a read spec points at, or None when nothing matches. ``read`` is a GET path (or ``command`` a program printing text, run by the
    harness, never the model); ``list_key``/``match`` pick the item; ``pick`` a field inside it; ``count`` counts the matching items instead of
    reading a number; ``yes_no`` reads a text that starts Yes or No as 1 or 0; ``regex`` captures the number (the first number by default)."""
    if spec.get("command"):
        import subprocess
        text = subprocess.run(list(spec["command"]), capture_output=True, text=True, timeout=int(spec.get("timeout", 120)), check=False).stdout
        found = re.search(spec.get("regex") or NUMBER, text)
        return float(found.group(1).replace(",", "")) if found else None
    payload = client._request("GET", str(spec["read"]))
    items = [i for i in items_of(payload, spec.get("list_key", "")) if matches(i, spec.get("match"), spec.get("min_len"))] if (spec.get("match") or spec.get("list_key")) else [payload]
    if spec.get("count"):
        if spec.get("distinct"):     # one per distinct normalised first line of this field: two goals holding the same text count once
            first = lambda i: re.sub(r"\W+", " ", str(pick_of(i, spec["distinct"]) or "").strip().split("\n")[0]).strip().lower()
            return float(len({first(i) for i in items}))
        return float(len(items))
    for item in items:
        value = pick_of(item, spec.get("pick"))
        text = value if isinstance(value, str) else json.dumps(value, default=str)
        if spec.get("yes_no"):
            found = re.search(r"\b(yes|no)\b", str(value or ""), re.I)
            return None if not found else 1.0 if found.group(1).lower() == "yes" else 0.0
        found = re.search(spec.get("regex") or NUMBER, text)
        if found:
            return float(found.group(1).replace(",", ""))
    return None


def read_value(spec, client):
    """A reference value for the judge: a number (``read_number``), or with ``text`` the picked text (clipped), or with ``file`` and ``grep`` the
    first line of a file containing the grep text."""
    if spec.get("file"):
        with open(spec["file"], encoding="utf-8") as handle:
            return next((line.strip()[:400] for line in handle if str(spec.get("grep", "")).lower() in line.lower()), None)
    if spec.get("text"):
        payload = client._request("GET", str(spec["read"]))
        for item in [i for i in items_of(payload, spec.get("list_key", "")) if matches(i, spec.get("match"))]:
            value = pick_of(item, spec.get("pick"))
            return str(value)[:400] if value is not None else None
        return None
    return read_number(spec, client)


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
SINGLE_MESSAGE_ITEM = ("A single message is correct and complete for this request: it answers or replies directly and promises or does nothing more. Do not apply a "
                       "status-then-report two-message standard, and do not apply any length limit the case's own criteria do not set.")


def sequence_note(case, messages, metric=None):
    """The text added to the judge's notes (and the rubric item) for this case: the messages in order, the metric block and the standard
    that applies (two roles for a case that acts, one message otherwise)."""
    lines = [f"MESSAGES IN ORDER: " + " | ".join(f"({m['n']}, {m['kind']}{', t=' + str(m['t']) + 's' if m.get('t') is not None else ''}) {m['text'][:700]}" for m in messages)]
    if metric:
        lines.append(f"METRIC {metric.get('name')}: before {metric.get('before')}, after {metric.get('after')}, delta {metric.get('delta')}, moved {metric.get('moved')}."
                     + (f" Computed from data: {metric.get('computed')}, equal: {metric.get('matches_computed')}." if "computed" in metric else "")
                     + " The metric is never claimed to have moved when it did not.")
    return " ".join(lines), (TWO_MESSAGE_ITEM if is_action_case(case) else SINGLE_MESSAGE_ITEM)


#: The first plain number of a text: not a part of a date or a time (a digit run followed by "-", ":" or "/" is skipped).
NUMBER = r"(?<![\d.-])(\d[\d,]*\.?\d*)(?![\d,.]*[-:/])"
FIGURE = re.compile(r"[$]?\d[\d,]*(?:\.\d+)?%?")


def figures_in(text):
    """The numbers a text states (amounts, counts, percentages, dates' digits), as plain strings without thousands separators or symbols."""
    found = set()
    for token in FIGURE.findall(str(text or "")):
        digits = token.strip("$%").replace(",", "")
        if digits and digits.replace(".", "", 1).isdigit():
            found.add(digits.rstrip("0").rstrip(".") if "." in digits else digits)
    return found


def unsupported_figures(reply, reads_text, message=""):
    """The figures a reply states that appear neither in the turn's own reads nor in the user's message: unsupported until shown otherwise."""
    return sorted(figures_in(reply) - figures_in(reads_text) - figures_in(message), key=lambda f: (len(f), f))


def reads_of(gathered, context_views, per_item=2500, total=24000):
    """What the turn actually read, as ``[{"source", "text"}]`` trimmed for the judge and the private log: each tool result or read observation
    the loop gathered, then the context the run was handed. Nothing here comes from any test data."""
    items = []
    for observation in gathered or []:
        if isinstance(observation, dict):
            text = observation.get("tool_text") or observation.get("text") or observation.get("content") or ""
            if str(text).strip():
                items.append({"source": str(observation.get("locator") or observation.get("rel_path") or observation.get("kind") or "read"), "text": str(text)[:per_item]})
    for view in context_views or []:
        if str(view).strip():
            items.append({"source": "context handed to the run", "text": str(view)[:per_item * 2]})
    out, used = [], 0
    for item in items:
        if used + len(item["text"]) > total:
            break
        out.append(item)
        used += len(item["text"])
    return out


def reads_text(reads):
    return "\n\n".join(f"## {r['source']}\n{r['text']}" for r in reads)


def load_rules(path):
    """``{case id: {"effect", "state_check", "metric_probe"}}`` from the rules file (JSON), or {} without one."""
    if not path:
        return {}
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def checks_of(value):
    """The structured state checks of a case as a list (a single dict, a list of dicts, or none)."""
    if isinstance(value, dict):
        return [value]
    return [v for v in value if isinstance(v, dict)] if isinstance(value, list) else []


def with_rules(case, rules):
    """The case with the structured rules of its id laid over a prose value (a structured value already on the case wins)."""
    extra = rules.get(case.get("id")) or {}
    merged = dict(case)
    for key in ("effect", "state_check", "metric_probe", "reference", "zero_writes", "tool_pins", "known_gaps"):
        if key in extra and (key in ("reference", "zero_writes", "tool_pins", "known_gaps") or structured(case.get(key)) is None):
            merged[key] = extra[key]
    return merged


def stamp(started):
    return round(time.time() - started, 1)
