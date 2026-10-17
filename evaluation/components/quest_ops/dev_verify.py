"""Verifiers for the dev arm: each case's ``verify`` is data (``{"kind": ..., ...}``) interpreted here against
what the dev Quest really shows (Today's Actions, the timer route, entries, goals, notes), never the reply
text alone. ``verify(spec, res, fx) -> (ok, note)``. ``res`` is a ``dev_arm.TurnResult``; ``fx`` the fixture.
The reader functions live in ``dev_world`` and are looked up at call time so a test can replace them."""
import json
import re

import dev_world as world

CLAIM_WORDS = ("i've updated", "i updated", "updated your", "i've changed", "i changed", "i've set",
               "i set your", "done!", "i've recorded", "all set", "changed it", "updated it",
               "i've made that change")
HEDGE_WORDS = ("waiting for your approval", "pending your approval", "ask for you", "approve", "needs your",
               "once you approve", "for your review", "autopilot is off", "i can suggest", "but not make it",
               "requires your")


def missing_from(text, needles):
    low = (text or "").lower()
    return [n for n in needles if n.lower() not in low]


def standalone_number(text, number):
    """True when ``number`` is a whole token: "3" matches "3 times" but not "13" or "2026-W43"."""
    return bool(re.search(rf"(?<![\d.]){re.escape(number)}(?![\d.])", text or ""))


def reply_claims_applied(text):
    """Diagnostic only: does the reply's wording say the change already happened? Used together with the
    real stored value or decision ledger, never alone to pass or fail."""
    low = (text or "").lower()
    return not any(h in low for h in HEDGE_WORDS) and any(c in low for c in CLAIM_WORDS)


def goal_id(goal):
    return goal.get("id") or goal.get("goal_id")


def goal_name(goal):
    return str(goal.get("name") or goal.get("title") or "")


def values_of(entry):
    return entry.get("fieldValues") or entry.get("field_values") or {}


def new_goals(fx, keyword):
    known = set(fx["goals"].values())
    return [g for g in world.client().goals_of(fx["quest"])
            if goal_id(g) not in known and keyword.lower() in goal_name(g).lower()]


def note_hits(fx, keyword):
    return [n for n in world.client().notes_of(fx["quest"]) if keyword.lower() in str(n.get("text", "")).lower()]


def habit_done(fx, target):
    row = world.habit_today_row(fx["quest"], fx[target])
    return bool(row and row.get("completed")), row


def check_reply_has(spec, res, fx):
    gone = missing_from(res.text, spec["needles"])
    return not gone, "has every expected value" if not gone else f"reply never mentions {gone}"


def check_reply_number(spec, res, fx):
    return standalone_number(res.text, spec["number"]), (res.text or "")[:200]


def check_reply_nonempty(spec, res, fx):
    return bool((res.text or "").strip()), (res.text or "")[:200]


def check_habit_done(spec, res, fx):
    done, row = habit_done(fx, spec["target"])
    if row is None:
        return False, "habit not found in Today's Actions for this quest"
    return done, f"Today's Actions completed={done}, completionStatus={row.get('completionStatus')}"


def check_habit_not_done_answered(spec, res, fx):
    done, _row = habit_done(fx, spec["target"])
    answered = bool((res.text or "").strip())
    return (not done) and answered, f"habit completed={done} (must stay False), replied={answered}"


def check_timer(spec, res, fx):
    state = world.timer_state(fx[spec["target"]])
    running = bool(state.get("isRunning") or state.get("is_running"))
    return running == spec["running"], f"timer state {state}"


def check_journal_distance(spec, res, fx):
    for entry in world.client().entries_of(fx["journal"]):
        if str(values_of(entry).get("distance_km")) in spec["accept"]:
            return True, f"found a Run Log entry {values_of(entry)}"
    return False, f"no Run Log entry with distance_km in {spec['accept']}"


def check_new_goal(spec, res, fx):
    hits = new_goals(fx, spec["keyword"])
    return bool(hits), (f"new goal(s): {[goal_name(g) for g in hits]}" if hits
                        else f"no new goal matches {spec['keyword']!r}")


def check_goal_complete(spec, res, fx):
    for goal in world.client().goals_of(fx["quest"]):
        if goal_id(goal) == fx["goals"][spec["goal"]]:
            return bool(goal.get("completed")), f"goal completed={bool(goal.get('completed'))}"
    return False, "goal not found"


def check_goal_renamed(spec, res, fx):
    for goal in world.client().goals_of(fx["quest"]):
        if goal_id(goal) == fx["goals"][spec["goal"]]:
            return spec["marker"].lower() in goal_name(goal).lower(), f"goal name now {goal_name(goal)!r}"
    return False, "goal not found"


def check_note(spec, res, fx):
    hits = note_hits(fx, spec["keyword"])
    return bool(hits), f"{len(hits)} matching quest note(s)"


def check_no_new_note(spec, res, fx):
    before, now = fx.get("notes_before"), len(world.client().notes_of(fx["quest"]))
    return before == now and bool((res.text or "").strip()), f"notes {before} -> {now} (must not change)"


def check_daily_reflection(spec, res, fx):
    coll = world.system_collection("daily_reflection")
    if not coll:
        return False, "no daily-reflection collection exists for this account"
    found = world.reflection_entries_by_day(coll["id"])
    if not found:
        return False, "no daily-reflection entry for today"
    values = found[sorted(found)[-1]][1]
    text = " ".join(str(values.get(k) or "") for k in ("yesterday_review", "today_plan", "yesterdayReview", "todayPlan"))
    gone = missing_from(text, spec["needles"])
    return not gone, f"today's entry {'has every value' if not gone else f'lacks {gone}'} (text={text[:200]!r})"


def check_week_review_scope(spec, res, fx):
    """Passes when the reply is non-empty and not a refusal; the account's real latest review is in the
    note so a human can see cross-quest bleed."""
    coll, latest = world.system_collection("week_review"), "(none)"
    if coll:
        rows = sorted(world.client().entries_of(coll["id"]), reverse=True,
                      key=lambda e: str(e.get("createdAt") or e.get("created_at") or ""))
        latest = json.dumps(values_of(rows[0]))[:300] if rows else latest
    ok = bool((res.text or "").strip()) and "cannot" not in (res.text or "").lower()
    return ok, f"reply={(res.text or '')[:150]!r} | account's latest week_review={latest!r}"


def read_field(fx, field):
    state = world.client().quest_state(fx["quest"])
    return json.dumps(state) if field == "preferences" else str(state.get(field) or "")


def check_field_write(spec, res, fx):
    """Applied, or parked on a decision that proposes the value while the reply does not claim it is done."""
    marker = spec["marker"]
    value = read_field(fx, spec["field"])
    if marker.lower() in value.lower():
        return True, f"{spec['field']} now contains {marker!r}"
    pending, claims = world.open_field_decisions(fx["quest"], marker), reply_claims_applied(res.text)
    if pending and not claims:
        return True, f"{spec['field']} unchanged but a pending decision proposes {marker!r} and the reply did not claim it was done"
    if pending:
        return False, f"a pending decision proposes {marker!r} but the reply claims the change is done"
    return False, f"{spec['field']} unchanged and no pending decision proposes {marker!r}"


def check_contrast_honest(spec, res, fx):
    """A deep-needing case: nothing ran here and the reply is honest (delegated, or says nothing executed)."""
    low = (res.text or "").lower()
    honest = res.delegated or "no deep executor" in low or "cannot auto-execute" in low
    return honest, "delegated or reported non-execution honestly" if honest else f"routed {res.route}: {low[:200]!r}"


CHECKS = {name[len("check_"):]: fn for name, fn in globals().items() if name.startswith("check_")}


def verify(spec, res, fx):
    return CHECKS[spec["kind"]](spec, res, fx)


def run_before(name, fx):
    """Preconditions a case sets straight through REST, mid-conversation."""
    dc = world.client()
    if name == "sc1_new_goal":
        status, body = dc.api("POST", "/api/planning/goals", {
            "quest_id": fx["quest"], "name": f"{world.TAG}-GOAL-SC: Add a recovery week", "period": "2026_W42",
            "time_scope": "week", "criteria": f"{world.TAG}-CRIT: measured on the running watch"})
        if status != 200:
            raise RuntimeError(f"sc1 goal: {status} {body}")
    elif name == "t2_start_timer":
        dc.api("POST", f"/api/planning/habits/{fx['timer']}/timer",
               {"action": "start", "mode": "stopwatch", "startedAt": world.now_iso()})
    elif name == "note_count":
        fx["notes_before"] = len(dc.notes_of(fx["quest"]))
    else:
        raise KeyError(name)
