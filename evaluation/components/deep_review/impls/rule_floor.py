"""BASELINE (offline): decide from thresholds and keywords alone. Stop when the tail names a usage or credit
limit, when the log is empty or idle for over 5 minutes with no sign of a machine at work (a build, test,
download, poll or a task that says to wait), when the worker says it is blocked on a person, or when the same
command line repeats three times. Everything else keeps running. Zero model calls. It was written with the 39
cases in view (no held-out set), so its score is an optimistic ceiling for a rule, not a blind result; a
model reviewer has to match it on these cases and be robust beyond them."""
import re

LIMIT = re.compile(r"usage limit|credit balance|quota|rate_limit|token budget|context window", re.I)
MACHINE_AT_WORK = re.compile(r"\b(watch|polling|poll|sleep|sleeping|build|building|download|downloading|training|"
                             r"epoch|in progress|still running|pending|waiting for ci|checks)\b", re.I)
BLOCKED_ON_PERSON = re.compile(r"waiting for your answer|cannot proceed without|please confirm|"
                               r"i am blocked|cannot continue until|need a token from the user|"
                               r"password is required", re.I)


def build(meter):  # noqa: ARG001
    def review(elapsed, idle, tail):  # noqa: ARG001
        if LIMIT.search(tail):
            return {"stop": True, "reason": "limit named in log"}
        if BLOCKED_ON_PERSON.search(tail):
            return {"stop": True, "reason": "blocked on a person"}
        commands = [ln for ln in tail.splitlines() if ln.startswith("- $")]
        if commands and any(commands.count(c) >= 3 for c in set(commands)):
            return {"stop": True, "reason": "same command three times"}
        if tail.startswith("(the session log is empty") and idle > 300:
            return {"stop": True, "reason": "empty log, long idle"}
        if idle > 300 and not MACHINE_AT_WORK.search(tail):
            return {"stop": True, "reason": "idle over 5 minutes, no machine at work"}
        return {"stop": False, "reason": "no stop condition"}
    return review
