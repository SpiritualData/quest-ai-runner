"""The value gate of a progress case: a turn asked to make progress passes only when it made measurable AI progress, ``value = D x G x L x M``.

It applies only to a case whose data declares progress work (``ai_doable`` present); every other case (a bookkeeping request, a question, a production
failure) is untouched, so a request that IS bookkeeping still passes when it does the bookkeeping well. It reads structured fields only: the tool calls the
turn ran (the replay records each call's arguments and result data), the writes it attempted, and the judge's per-item verdict. Nothing reads the reply's words.

- D (deliverable): a tool result carries an ``increment`` whose ``kind`` is one the case lists as AI-doable (deliverable, metric_read, evidence, recommendation,
  simulation), with a ``measure_id``.
- G (grounded and good): every ``deliverable_rubric`` item passes in the judge's verdict, and the verdict names no invented fact.
- L (landed): the increment was kept where the done test says (a kept goal whose title starts with ``increment_measure.lands_on.title_prefix``),
  with at most one goal created.
- M (measured): the increment has a ``before`` and an ``after`` measured by a method other than the writer's say-so (``read``, ``rubric``,
  ``evidence``, ``quality_score``, ``simulation``), a claimed change shows in the readings, a simulation is labelled as one, and the milestone metric was read
  again after the increment was kept.

A turn whose only writes set an owner, a due date or a reminder fails D whatever it says, and so does a human ask with no increment.
"""
import json
import re
from typing import Any, Dict, Iterable, List, Mapping, Optional

MEASURED_METHODS = ("read", "rubric", "evidence", "quality_score", "simulation")
METRIC_READ_TOOLS = ("read_spec_records", "read_metric_source", "read_recorded_metric_value")
KEEP_TOOLS = ("keep_milestone_deliverable",)
DELIVERABLE_ITEM = "DELIVERABLE RUBRIC: "
INVENTED = "hallucinated_fact"


def applies(case: Mapping[str, Any]) -> bool:
    """True for a case whose data declares progress work (the ``ai_doable`` field present and not empty)."""
    return bool(case.get("ai_doable"))


def tool_of(read: Mapping[str, Any]) -> str:
    """The tool name of one recorded read (``"tool <name> {...}"``), else ""."""
    found = re.match(r"tool (\S+)", str(read.get("source") or ""))
    return found.group(1) if found else ""


def data_of(read: Mapping[str, Any]) -> Optional[Any]:
    """The structured data a tool result carried (the replay appends it as one JSON line after the text), else None."""
    for line in reversed(str(read.get("text") or "").splitlines()):
        line = line.strip()
        if line.startswith(("{", "[")):
            try:
                return json.loads(line)
            except ValueError:
                return None
    return None


def increments_in(reads: Iterable[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    """``[{"index", "tool", "increment", "data"}]`` for every tool result that reports an increment, in order."""
    out = []
    for k, read in enumerate(reads or []):
        data = data_of(read)
        if isinstance(data, dict) and isinstance(data.get("increment"), dict):
            out.append({"index": k, "tool": tool_of(read), "increment": data["increment"], "data": data})
    return out


def number(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def goals_created(writes: Iterable[Mapping[str, Any]]) -> int:
    return sum(1 for w in writes or [] if w.get("kind") != "housekeeping" and "goal" in str(w.get("collection") or "").lower()
               and str(w.get("method") or "").startswith("insert"))


def rubric_passes(verdict: Mapping[str, Any], case: Mapping[str, Any]) -> Optional[bool]:
    """Whether every deliverable rubric item passed in the verdict (None when the verdict holds none of them)."""
    items = [r for r in (verdict or {}).get("rubric") or [] if str(r.get("item") or "").startswith(DELIVERABLE_ITEM)]
    if not items:
        return None
    return all(r.get("pass") for r in items)


def evaluate(case: Mapping[str, Any], reads: Iterable[Mapping[str, Any]], writes: Iterable[Mapping[str, Any]], verdict: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    """``{"applies", "value", "D", "G", "L", "M", "why"}``. ``value`` is 1 only when all four hold; a case the gate does not apply to has value None."""
    if not applies(case):
        return {"applies": False, "value": None, "why": "not a progress case"}
    reads = list(reads or [])
    kinds = {str(item.get("kind") or "") for item in case.get("ai_doable") or [] if isinstance(item, dict)}
    found = [f for f in increments_in(reads) if str(f["increment"].get("kind") or "") in kinds and f["increment"].get("measure_id")]
    why: List[str] = []
    d = bool(found)
    if not d:
        why.append("D: no AI increment of an AI-doable kind with a measure was made (bookkeeping or a human ask alone is not progress)")
    best = found[-1] if found else None
    inc = best["increment"] if best else {}
    passed = rubric_passes(verdict or {}, case)
    invented = (verdict or {}).get("failure_class") == INVENTED
    need_rubric = bool(case.get("deliverable_rubric"))
    g = d and not invented and (passed is True or (not need_rubric and passed is None))
    if d and not g:
        why.append("G: " + ("the judge found an invented fact" if invented else "the deliverable does not meet its rubric" if passed is False else "the deliverable rubric was not judged"))
    lands = ((case.get("increment_measure") or {}).get("lands_on") or {}) if isinstance(case.get("increment_measure"), dict) else {}
    prefix = str(lands.get("title_prefix") or "")
    data = best["data"] if best else {}
    goal = data.get("goal") if isinstance(data.get("goal"), dict) else {}
    kept_title = str(goal.get("name") or goal.get("title") or data.get("title") or "")
    state = str(data.get("state") or "")
    lands_ok = state in ("created", "updated", "unchanged") and (not prefix or kept_title.startswith(prefix))
    l_ok = d and lands_ok and goals_created(writes) <= 1
    if d and not l_ok:
        why.append(f"L: the increment was not kept on the milestone's keyed goal (state {state or 'none'}, title {kept_title!r}, prefix {prefix!r})")
    before, after = number(inc.get("before")), number(inc.get("after"))
    method = str(inc.get("method") or "")
    m = d and before is not None and after is not None and method in MEASURED_METHODS
    if d and not m:
        why.append(f"M: the increment has no measured before and after (before {inc.get('before')!r}, after {inc.get('after')!r}, method {method!r})")
    if m and inc.get("changed") is True and after == before:
        m = False
        why.append("M: the increment claims a change the readings do not show")
    if m and method == "simulation" and not inc.get("simulated"):
        m = False
        why.append("M: a simulation reading is not labelled as one")
    if m:
        reread = [k for k, read in enumerate(reads) if tool_of(read) in METRIC_READ_TOOLS and k > best["index"]]
        if not reread and not isinstance(data.get("milestone_metric"), dict):
            m = False
            why.append("M: the milestone metric was not read again after the increment")
    value = int(d and g and l_ok and m)
    return {"applies": True, "value": value, "D": int(d), "G": int(bool(g)), "L": int(bool(l_ok)), "M": int(bool(m)), "why": "; ".join(why) or "all four hold",
            "increment": inc or None}


def rubric_items(case: Mapping[str, Any]) -> List[str]:
    """The deliverable rubric as judge rubric items (each marked, so the gate finds its verdict)."""
    return [DELIVERABLE_ITEM + str(item) + " (judged on the deliverable the turn kept, as its tool call shows)" for item in case.get("deliverable_rubric") or []]
