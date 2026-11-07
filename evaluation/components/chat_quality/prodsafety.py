"""The production safety net shared by every production arm (``prod_quest_chat``, ``prod_qar``, ``prod_qar_readonly``): nothing a run creates may need a human to undo.

Around one case: refuse to start a write-capable case in an autopilot window, print what the case may create and how each kind is undone, fingerprint the quest
before and after, take back what is provably this run's and not the case's intended result, and report anything else (an ambiguous or un-undoable change) as left,
which fails the case. All decisions are in ``prodchat.py`` as pure functions; this class only sequences them for an arm and is offline-tested with a fake client.
"""
import json
import time

import prodchat as PC


class TurnSafety:
    def __init__(self, client, ai_user_id, case_id, effect, zero_writes, message, clock=time.time, sleep=time.sleep, say=print):
        self.client, self.ai_user_id, self.case_id = client, ai_user_id, case_id
        self.effect, self.zero, self.message = effect, bool(zero_writes), message
        self.clock, self.sleep, self.say = clock, sleep, say
        self.before = None
        self.started = None

    RATE_WAIT = 60

    def retry_once(self, read):
        """A rate-limited read is not a result: wait, then try once more; a second 429 raises ``RateLimited`` (the arm reports 'not run: rate limited')."""
        try:
            return read()
        except PC.RateLimited as first:
            self.say(f"  {self.case_id}: rate limited ({first}); waiting {self.RATE_WAIT}s and retrying once")
            self.sleep(self.RATE_WAIT)
            return read()

    def gate(self):
        """None when the case may start; otherwise the 'autopilot window' reason (a write-capable case only, after waiting up to 15 minutes in short sleeps)."""
        if self.zero:
            return None
        return self.retry_once(lambda: PC.wait_for_clear(self.client, now=self.clock, sleep=self.sleep))

    def begin(self):
        self.say(f"  {self.case_id}: {PC.describe_undo(self.effect, self.zero)}")
        self.before = self.retry_once(lambda: PC.read_fingerprint(self.client))
        self.started = self.clock()
        return self.before

    def finish(self, own_conv=None, frames=None):
        """Compare, clean and report. ``frames`` is everything the turn emitted (its text is searched for ids). Returns a dict with ``changes``, ``unexpected``,
        ``cleanup`` (the result), ``line`` (the cleanup text) and ``failed`` (anything left, or any unexpected write)."""
        ended = self.clock()
        after = self.retry_once(lambda: PC.read_fingerprint(self.client))
        changes = PC.diff_fingerprints(self.before, after, own_conv=own_conv)
        try:
            from quest_ai_runner.the_world.tools import dry_run_writes
            titles = set(dry_run_writes.RECORDER.call_titles)
            blocked = list(dry_run_writes.RECORDER.unexpected)
        except Exception:  # noqa: BLE001
            titles, blocked = set(), []
        unexpected = PC.unexpected_writes(changes, self.effect, self.zero, titles) + blocked
        window = (self.started - 5, ended + 30)
        evidence = json.dumps(frames, default=str) if frames else ""
        plan = PC.plan_cleanup(changes, own_conv, window, self.ai_user_id, self.effect, self.zero, evidence, self.message, titles)
        result = PC.run_cleanup(self.client, plan)
        return dict(changes=changes, unexpected=unexpected, cleanup=result, line=PC.cleanup_line(result), failed=bool(result["left"] or unexpected))

    def apply(self, record, outcome):
        """Write the outcome into the case's record: the cleanup line (in 'Lane gaps', the sheet column an arm may fill; 'Notes' is a human's), the unexpected writes, and the
        failure rule (anything left, or any write the case did not authorise, fails the case). A failed cleanup is printed loudly."""
        record["cleanup"], record["unexpected_writes"] = outcome["line"], outcome["unexpected"]
        gaps = [outcome["line"]] if (outcome["cleanup"]["withdrawn"] or outcome["cleanup"]["deleted"] or outcome["cleanup"]["left"]) else []
        record["lane_gaps"] = sorted(set(list(record.get("lane_gaps") or []) + gaps))
        if outcome["failed"]:
            record["passed"] = False
        if outcome["cleanup"]["left"]:
            self.say(f"  {self.case_id}: CLEANUP FAILED OR AMBIGUOUS, a human must look: {outcome['line']}")
        return record
