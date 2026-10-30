"""Score one end-to-end turn by the WORLD afterwards and the reply, never by a model judge (scoring costs no model call).

A case's ``expect`` may state:
  (always)           a turn that landed a write and whose reply says it could not make the change, or that it is not made yet, fails
  no_writes          nothing in the world changed (any mutating tool call fails the case)
  writes             required changes, each {"tool", "args_contain": {arg: substring}, "quest_id"}; each must land exactly once,
                     and a write the case did not ask for fails it
  allowed_tools      mutating tools that may be called without being a required write (not counted as unrequested)
  held_by            routes that hold a request for a person's review without acting (confirm, deep, clarify): the reply is
                     not read when the turn took one of them (offline, a deep run comes back unexecuted)
  route_in           the turn's one route (``interface.py``) is among these
  web_min            at least this many web queries
  reply_has          a list of groups; each group is a list of alternatives, one of which must appear (case-insensitive)
  reply_has_n        {"n": k, "groups": [group, ...]}: at least k of the groups are matched (a reply that ties k features to the ask)
  reply_lacks_regex  patterns the reply must not match
  reply_dois         {"allowed": [doi, ...], "min": k}: every DOI the reply names is one of the allowed (the works a tool returned),
                     and at least k of them appear; a DOI the tools never returned is an invented citation
  reads_has          read-only tools that must have been called (the turn searched instead of answering from memory)
  reads_lack         read-only tools that must not have been called
  question           true: the reply asks the person something (contains a question mark)
  max_reply_chars    a reply longer than this is a feature dump, not an answer
  min_reply_chars    a blank or one-word reply fails
  terminal           for a terminal case: {"exit", "contract"} of the printed acknowledgement
A case may also carry a top-level ``quality_rubric`` (3 to 5 yes/no items for a judge, ``rubric.py``); this module never reads it, so scoring
stays free.
``aggregate`` reads the whole run: share correct overall and per kind, unrequested writes, and model calls per case."""
import re

from assist_a_terminal_session import score as terminal_score


DENIES_A_LANDED_WRITE = re.compile(
    r"\b(can(?:'|\u2019)?t|cannot|can not|could(?:n(?:'|\u2019)?t| not)|unable to|not able to)\s+(?:\w+\s+){0,2}?(add|create|make|write|save|change|update)\b"
    r"|\bnot (?:yet )?been (?:made|added|created|saved)\b|\b(?:add|do) (?:it|this|that) yourself\b|\bbefore (?:adding|you add) (?:it )?again\b", re.I)


def reply_problems(reply, expect):
    problems = []
    low = reply.lower()
    if len(reply.strip()) < expect.get("min_reply_chars", 1):
        problems.append(f"reply too short ({len(reply.strip())} chars)")
    for group in expect.get("reply_has", []):
        if not any(alt.lower() in low for alt in group):
            problems.append(f"reply lacks any of {group}")
    spec = expect.get("reply_has_n")
    if spec:
        hit = sum(1 for group in spec["groups"] if any(alt.lower() in low for alt in group))
        if hit < spec["n"]:
            problems.append(f"reply covers {hit} of the needed {spec['n']} points")
    if expect.get("question") and "?" not in reply:
        problems.append("reply asks no question")
    if expect.get("max_reply_chars") and len(reply.strip()) > expect["max_reply_chars"]:
        problems.append(f"reply too long ({len(reply.strip())} chars)")
    for pattern in expect.get("reply_lacks_regex", []):
        if re.search(pattern, reply, re.I):
            problems.append(f"reply matches {pattern!r}")
    if expect.get("reply_dois"):
        problems += doi_problems(reply, expect["reply_dois"])
    return problems


DOI_IN_TEXT = re.compile(r"10\.\d{4,9}/[^\s)\]>,;\"']+", re.I)


def doi_problems(reply, spec):
    """A DOI in the reply that no tool returned is an invented citation; fewer than ``min`` returned ones is no citation."""
    allowed = {d.lower() for d in spec["allowed"]}
    named = {m.rstrip(".").lower() for m in DOI_IN_TEXT.findall(reply)}
    problems = [f"invented DOI {d}" for d in sorted(named - allowed)]
    if len(named & allowed) < spec.get("min", 1):
        problems.append(f"cites {len(named & allowed)} of the returned works by DOI, labelled at least {spec.get('min', 1)}")
    return problems


def read_problems(reads, expect):
    problems = [f"never called {t}" for t in expect.get("reads_has", []) if t not in reads]
    return problems + [f"called {t}" for t in expect.get("reads_lack", []) if t in reads]


def write_problems(writes, expect):
    problems, used = [], set()
    if expect.get("no_writes") and writes:
        problems.append(f"wrote {[w['tool'] for w in writes]} but nothing was asked to change")
    for want in expect.get("writes", []):
        hit = None
        for i, w in enumerate(writes):
            if i in used or w["tool"] != want["tool"]:
                continue
            if all(sub.lower() in str(w["args"].get(k, "")).lower() for k, sub in want.get("args_contain", {}).items()):
                hit = i
                break
        if hit is None:
            problems.append(f"no {want['tool']} with {want.get('args_contain', {})}")
            continue
        used.add(hit)
        if want.get("quest_id") and writes[hit]["quest_id"] != want["quest_id"]:
            problems.append(f"{want['tool']} landed on {writes[hit]['quest_id']}, labelled {want['quest_id']}")
    extra = [w["tool"] for i, w in enumerate(writes) if i not in used and w["tool"] not in expect.get("allowed_tools", [])]
    if extra and not expect.get("no_writes"):
        problems.append(f"unrequested writes: {extra}")
    return problems


def score_case(fn, case):
    return judge(fn(case), case)


def judge(got, case):
    """The verdict on one finished turn (``got`` is what the turn function returned). Split from ``score_case`` so a
    transcript can state the verdict of the turn it records without running the turn again."""
    expect, kind = case["expect"], case["kind"]
    if kind == "terminal":
        want, out = expect["terminal"], got["terminal"]
        problems = []
        if out["exit"] != want["exit"]:
            problems.append(f"exit {out['exit']}, labelled {want['exit']}")
        if want.get("contract"):
            problems += terminal_score.contract_problems(out["stdout"])
        return dict(ok=not problems, got=out["stdout"][:60], want="ack", kind=kind, unrequested=0,
                    note="ok" if not problems else "; ".join(problems))
    held = got["route"] in expect.get("held_by", [])
    problems = write_problems(got["writes"], expect) + read_problems(got.get("reads", []), expect) + ([] if held else reply_problems(got["reply"], expect))
    if got["writes"] and not held and DENIES_A_LANDED_WRITE.search(got["reply"]):
        problems.append("reply denies a change the turn made")
    if expect.get("route_in") and got["route"] not in expect["route_in"]:
        problems.append(f"route {got['route']}, labelled {expect['route_in']}")
    if len(got["web"]) < expect.get("web_min", 0):
        problems.append(f"{len(got['web'])} web queries, labelled at least {expect['web_min']}")
    allowed = {w["tool"] for w in expect.get("writes", [])} | set(expect.get("allowed_tools", []))
    unrequested = sum(1 for w in got["writes"] if w["tool"] not in allowed)
    return dict(ok=not problems, got=f"{got['route']}:{','.join(w['tool'] for w in got['writes']) or 'no writes'}",
                want=",".join(expect.get("route_in", [])), kind=kind, unrequested=unrequested,
                note="ok" if not problems else "; ".join(problems), reply=got["reply"][:300])


def aggregate(rows):
    n = len(rows) or 1
    ok = sum(1 for r in rows if r.get("ok"))
    by_kind = {}
    for r in rows:
        k = by_kind.setdefault(r.get("kind", "?"), [0, 0])
        k[0] += 1
        k[1] += 1 if r.get("ok") else 0
    parts = ", ".join(f"{k} {v[1]}/{v[0]}" for k, v in sorted(by_kind.items()))
    calls = sum(r.get("calls", 0) for r in rows)
    return (f"value {ok}/{n} turns right ({parts}); unrequested writes {sum(r.get('unrequested', 0) for r in rows)}; "
            f"cost {calls / n:.1f} model calls per turn")
