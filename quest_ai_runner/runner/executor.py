"""Executor — run ONE claimed Quest task through the brain + goal-runner, report the result.

Given a single claimed assistant-task, the executor:
  1. Builds the request (the task text) + a small context view.
  2. Runs it through ``core.Orchestrator`` with the configured adapters.
  3. Maps the OrchestratorResult onto the Quest task callback:
       - answer            -> PATCH done   + result (the answer text)
       - deep (met)        -> PATCH done   + result (the run output / summary)
       - deep (not met)    -> PATCH failed | needs_you  (limit/error -> failed; raised a
                              decision -> needs_you with the decision_id)
       - confirm           -> a decision-request was raised -> PATCH needs_you + decision_id
       - cancelled         -> NO PATCH (the backend already set status=cancelled and appends its
                              own terminal chat message; a PATCH here would just 409) -- a
                              best-effort progress note only.
  4. Never raises to the poller: any error becomes a PATCH failed with the message, unless the
     task was cancelled meanwhile (see above).

It does NOT claim or discover (the poller owns that) — it is the unit of work for one task, so
it can be unit-tested against a mock Quest client + stub brain with no network.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from ..core.adapters import Mode, ProgressEvent
from ..core.orchestrator import Orchestrator, OrchestratorResult

log = logging.getLogger("quest-ai-runner.executor")

# How often (seconds of real time) the throttled cancel_check built by ``_build_cancel_check`` is
# allowed to actually call the Quest API. The orchestrator may poll cancel_check at every loop
# boundary (frequently), but cancellation is a rare, human-triggered event -- hammering the API on
# every check would waste calls for no benefit.
CANCEL_CHECK_INTERVAL_SECONDS = 15.0

# How many of a goal's most recent notes ride along as run context, and how many of the PERSON's
# own notes are guaranteed a place among them (see ``render_goal_notes``).
NOTE_CONTEXT_LIMIT = 8
PERSON_NOTE_FLOOR = 3

def email_contract(quest_id: str, rep_id: Optional[str] = None) -> str:
    """Told to a run whose quest has email switched on.

    Two failures this heads off. A run that mails through a local script sends something Quest
    never saw: no per-quest Reply-To, so the person's answer goes nowhere; no unsubscribe handling;
    no record; and a signature naming a generic assistant rather than the persona that did the
    work. And with delivery now automatic, a run that ALSO mails by hand sends the person the same
    thing twice.
    """
    rep = f" --rep {rep_id}" if rep_id else ""
    return (
        "Email for this quest is ON. What you put in your result is mailed to its people "
        "automatically, so do NOT send mail yourself with a local mail script -- that mail would "
        "carry no reply address, and the person would receive your work twice. If you need to send "
        "something at a particular moment instead of at the end, run:\n"
        f"  python -m quest_ai_runner.tools.send_quest_email --quest {quest_id} "
        f"--subject \"<subject>\" --body-file <path>{rep}\n"
        "Recipients come from the quest's own settings: you choose the words and the moment, never "
        "the audience.\n"
        "Write the result AS the message they will read, in markdown, and nothing else. No "
        "\"here is what I sent\" preamble, no second copy of the text pasted underneath, no note "
        "to yourself about which delivery path you used: the result IS the mail, so anything else "
        "in it is something a person reads in their inbox and has to skip past."
    )


# How a good colleague behaves around a question they had to ask. Raising one is not a reason to
# stop: they get on with everything that does not depend on the answer, and say what is waiting on
# it. A run that downs tools the moment it needs something turns one unanswered message into a
# stalled week, and the person finds out only when nothing has moved.
#
# The distinction that matters when the answer still has not come: ASKING again is right, FILING
# again is not. Someone who has not acted needs reminding, so the same ask belongs in tomorrow's
# brief; but a second decision-request for it leaves two rows to resolve for one question, and the
# queue fills with duplicates of a thing they have already seen.
KEEP_GOING_CONTRACT = (
    "If you raise a question, need a decision, or hit a blocker: do NOT stop there. Carry on with "
    "everything that does not depend on the answer, and finish by naming what is still waiting on "
    "them and what you did in the meantime. If an earlier run already asked for the same thing and "
    "it is still unanswered, SAY SO AGAIN in what you produce -- someone who has not got to it "
    "needs reminding, not silence -- but do NOT file a second request for it: refer to the one "
    "already open instead of creating a duplicate.\n"
    "The same holds for anything you asked THEM to do. An outstanding item stays outstanding: name "
    "it first, say how long it has been waiting, and do not quietly replace it with today's new "
    "thing. Moving on silently is how the first item is missed entirely -- they cannot chase what "
    "you stopped mentioning."
)


# Stated to any run that can see the person's own words, because the reply loop only closes if
# both halves hold: the run has to answer what they said, and it has to leave the thing they can
# answer NEXT time where they will find it.
#
# The second half is the one that silently fails. A run that mails a brief, writes a file or posts
# elsewhere leaves Quest holding a DESCRIPTION of the work ("sent the brief, id 951f6..."), so the
# person opens their quest to reply and there is nothing there to reply to -- the content they
# actually read lives in their inbox. Quest is where they answer, so Quest gets the real copy.
REPLY_LOOP_CONTRACT = (
    "Closing the loop with the person: if any of the notes or captures above are theirs, open your "
    "result by saying what you did about them, so they can tell their reply landed. And when you "
    "produce something for them to READ (a brief, a summary, a document, an email), put its full "
    "text in your result, not a description of it -- Quest is where they see and answer your work, "
    "so anything you send or write elsewhere is a copy of what belongs there."
)


# What earlier runs did, INCLUDING the ones that failed. A failed run is not an empty one: it may
# have written files, sent mail, or resolved half the work before its goal could be confirmed, and
# the next run that cannot see that redoes it, contradicts it, or reports it as still pending.
RUN_HISTORY_HEADER = (
    "Earlier runs on this quest, oldest first. `failed` means the goal could not be CONFIRMED, not "
    "that nothing happened -- a failed run may have written files, sent mail, or finished most of "
    "the work. Read what it actually did before repeating or contradicting it."
)

# The person is not a subroutine: asking them to do something does not make it done.
NO_ASSUMED_PROGRESS_CONTRACT = (
    "Do NOT assume the person did anything you asked for previously. Confirm it from evidence: a "
    "goal or habit marked done, a note in their own words, or a change you can actually see in "
    "their files. With no evidence, treat it as NOT done, say so plainly, and carry it forward "
    "rather than moving on as though it happened."
)


def render_run_history(tasks: Optional[List[Dict[str, Any]]], limit: int = 6) -> str:
    """Recent runs on this quest, with what each one produced."""
    rows = [t for t in (tasks or []) if (t or {}).get("status") in
            ("done", "failed", "needs_you", "in_progress")]
    rows = sorted(rows, key=lambda t: str(t.get("updated_at") or ""))[-limit:]
    if not rows:
        return ""
    lines = []
    for task in rows:
        when = str(task.get("updated_at") or "")[:10]
        title = (task.get("title") or (task.get("text") or "")[:60] or "task").strip()
        result = " ".join(str(task.get("result") or "").split())[:300]
        line = f"  • [{when}] {task.get('status')}: {title}"
        if result:
            line += f"\n      produced: {result}"
        lines.append(line)
    return f"{RUN_HISTORY_HEADER}\n" + "\n".join(lines)


def render_goal_notes(notes: Optional[List[Dict[str, Any]]]) -> str:
    """Render a goal's recent notes for a run, saying WHO wrote each one.

    This is the reply channel. A person reads what a run produced, answers on the goal ("did the
    reading, skipped the writing, do X instead"), and the next run has to act on that. Two things
    broke it, and both are fixed here:

    1. **Attribution was dropped.** Notes went into the prompt as a flat bullet list, so a run could
       not tell the person's instructions from its OWN previous output. On a goal where the runner
       writes a note every day, that means a run reads a dozen of its own summaries, cannot see
       which one line came from the human, and treats a correction as just more of its own prior
       reasoning. Each note now carries its author and date, and human notes are marked as the
       person's own words, which is the whole point of a reply.

    2. **The person's note fell out of the window.** A plain "most recent N" cut is dominated by AI
       notes precisely on the goals that run often, so yesterday's human correction ages out while
       five machine summaries stay. The most recent ``PERSON_NOTE_FLOOR`` human-authored notes are
       therefore kept regardless of where they land in the ordering.

    Authorship comes from the backend's ``author_kind`` (``"user"`` for a signed-in human,
    ``"ai"`` for an API-key caller). A note with no ``author_kind`` at all is left unlabeled rather
    than guessed at: an older backend, or a note predating attribution, must not be asserted to be
    the person's instruction.
    """
    rows = [n for n in (notes or []) if (n or {}).get("text")]
    if not rows:
        return ""

    def is_person(note: Dict[str, Any]) -> bool:
        return str(note.get("author_kind") or "").lower() == "user"

    kept = rows[-NOTE_CONTEXT_LIMIT:]
    if len(rows) > len(kept):
        floored = [n for n in rows if is_person(n)][-PERSON_NOTE_FLOOR:]
        missing = [n for n in floored if n not in kept]
        if missing:
            # Oldest-to-newest overall, so the run still reads them in chronological order.
            kept = [n for n in rows if n in missing or n in kept]

    lines = []
    for note in kept:
        kind = str(note.get("author_kind") or "").lower()
        name = str(note.get("author_name") or "").strip()
        if kind == "user":
            who = f"{name}, the person" if name else "the person"
        elif kind == "ai":
            who = f"{name} (AI)" if name and name.lower() != "ai assistant" else "AI"
        else:
            who = name or "unattributed"
        when = str(note.get("created_at") or "")[:10]
        stamp = f"[{when}] " if when else ""
        lines.append(f"  • {stamp}({who}) {note['text']}")

    header = ("Goal notes, oldest first. Notes marked \"the person\" are the goal owner's own "
              "words: treat them as instructions that override anything an AI note claims.")
    return f"{header}\n" + "\n".join(lines)

# Same throttle, same reasoning, for the ``pending_inputs`` callable built by
# ``_build_pending_inputs``: a human typing a mid-task steering message is rare and not
# latency-sensitive down to the second, so claiming on every internal loop boundary would hammer
# the Quest API for no benefit. A real message is never lost by the throttle, only delayed by up
# to this many seconds until the next allowed claim.
MESSAGE_CLAIM_INTERVAL_SECONDS = 15.0

# Hard cap on the FALLBACK prior-conversation read in ``_build_context_view`` (the path taken only
# when no ConversationStore is wired). Without a cap, a long-running conversation would dump its
# entire transcript into every linked task's prompt, growing without bound as the conversation
# grows. Passed as ``max_bytes`` to ``read_section`` so the serving adapter truncates at the
# source; adapters that serve conversations keep the recent tail when truncating (see
# ``ClaudeConversationsAdapter.read_section``).
CONV_CONTEXT_MAX_BYTES = 16_000

# The value that marks a task as the recurring AUTOPILOT PASS (see ``runner.autopilot``).
AUTOPILOT_PASS_KIND = "autopilot"


def _is_autopilot_pass(task: Dict[str, Any]) -> bool:
    """Whether this task is the recurring autopilot pass (routed to ``AutopilotPass``).

    Reads ``task_kind`` FIRST, and that ordering is the whole point. ``task_kind`` is a PERSISTENT
    classification: the Quest API writes it once at create and never touches it again, and a
    recurring series' spawned occurrences inherit it. ``handler``, by contrast, is the CLAIMING
    WORKER'S OWN LABEL -- the poller stamps it on every ``claim()`` with the rep slug or runner
    label, OVERWRITING whatever was there. So routing on ``handler`` alone is unsound: it survives
    only because each recurring occurrence happens to be a fresh document, and it breaks the moment
    a task is re-polled, retried, or resumed after a claim (the claim label has replaced
    "autopilot", and the pass task would then be run as an ordinary deep task).

    ``handler`` is still accepted as a BACK-COMPAT fallback so a pass task queued before the
    backend gained ``task_kind`` still routes correctly.
    """
    if str(task.get("task_kind") or "").strip().lower() == AUTOPILOT_PASS_KIND:
        return True
    return str(task.get("handler") or "").strip().lower() == AUTOPILOT_PASS_KIND


@dataclass
class ExecutionOutcome:
    task_id: str
    status: str                       # "done" | "needs_you" | "failed" | "cancelled"
    result: str = ""
    decision_id: Optional[str] = None


class _TaskProgressSink:
    """Routes orchestrator events to the task's live progress stream.

    Forwards all events EXCEPT raw streaming partials to report_progress, so the
    task-detail SSE stream shows step-by-step what the AI is doing (plan -> read ->
    answer) and live token counts. Milestones additionally post into the originating
    chat (same behavior as the old MilestoneSink path).
    """
    _SKIP = frozenset({"partial"})

    def __init__(self, task_id: str, report_fn: Callable, on_milestone: Optional[Callable]):
        self._task_id = task_id
        self._report = report_fn
        self._on_milestone = on_milestone

    def update(self, event: ProgressEvent, mode) -> None:
        if event.type in self._SKIP:
            return
        try:
            self._report(self._task_id, event.type, text=event.text, data=event.data)
        except Exception:  # noqa: BLE001
            pass
        try:
            if event.type == "milestone" and self._on_milestone:
                self._on_milestone(event)
        except Exception:  # noqa: BLE001
            pass


class TaskExecutor:
    def __init__(self, client, orchestrator: Orchestrator, *,
                quest_folder_map: Optional[Dict[str, str]] = None,
                autopilot_pass: Optional[Any] = None):
        self._client = client
        self._orch = orchestrator
        # Cache the retrieval adapter from the orchestrator so _build_context_view can fetch
        # conversation history when conv_id is present
        self._retrieval = getattr(orchestrator, "retrieval", None)
        # {goal_or_quest_id: folder} -- see _resolve_working_dir. Same map the poller consults for
        # QUEST_SYNC.md sync (RunnerConfig.quest_folder_map); None/empty = every task uses the
        # deep-runner's configured global working_dir, exactly as before this feature existed.
        self._quest_folder_map = quest_folder_map or {}
        # The consumer's AutopilotPass (see runner/autopilot.py), wired by the poller. A task whose
        # ``handler == "autopilot"`` is routed to it instead of a normal deep run (see execute()).
        # None (no consumer wiring, or a runner that never sees such a task) -> untouched behavior.
        self._autopilot = autopilot_pass

    @staticmethod
    def _task_text(task: Dict[str, Any]) -> str:
        return (task.get("text") or task.get("title") or task.get("description") or "").strip()

    def _build_cancel_check(self, task_id: str,
                            interval: float = CANCEL_CHECK_INTERVAL_SECONDS) -> Callable[[], bool]:
        """Build a THROTTLED ``cancel_check`` callable to pass into ``Orchestrator.run()``.

        The orchestrator may poll this at every internal loop boundary (plan/gather/replan step,
        each deep-goal retry attempt) -- far more often than a cancellation could plausibly happen
        (a human hitting "stop" is rare and not latency-sensitive). Calling ``is_task_cancelled``
        on every poll would hammer the Quest API for no benefit, so this calls it AT MOST once per
        ``interval`` seconds of real time and returns the last known answer in between. Falls back
        to an always-False check when there's no task id or the client lacks the method (older
        clients / mocks), so the run behaves exactly as before.
        """
        if not task_id:
            return lambda: False
        is_cancelled = getattr(self._client, "is_task_cancelled", None)
        if not callable(is_cancelled):
            return lambda: False
        state = {"checked_at": 0.0, "cancelled": False}

        def _check() -> bool:
            now = time.monotonic()
            if now - state["checked_at"] >= interval:
                state["checked_at"] = now
                try:
                    state["cancelled"] = bool(is_cancelled(task_id))
                except Exception:  # noqa: BLE001 -- a check must never crash a run
                    pass
            return state["cancelled"]

        return _check

    # --- mid-run steering messages (human sends a message while this task runs) --------------

    def _claim_task_messages(self, task_id: str) -> List[Dict[str, Any]]:
        """Best-effort: claim any pending mid-task messages for ``task_id`` via the Quest client.

        No-ops (returns ``[]``) when there's no task id or the client lacks
        ``claim_task_messages`` (older clients / mocks), and never raises -- the client's own
        method is already best-effort by contract, but this call site guards too so a claim
        failure can never affect the run.
        """
        if not task_id:
            return []
        claim = getattr(self._client, "claim_task_messages", None)
        if not callable(claim):
            return []
        try:
            return claim(task_id) or []
        except Exception:  # noqa: BLE001
            return []

    @staticmethod
    def _message_texts(claimed: List[Dict[str, Any]]) -> List[str]:
        """Extract non-empty ``text`` fields from a list of claimed message dicts, in order."""
        texts = [str(m.get("text") or "").strip() for m in (claimed or [])]
        return [t for t in texts if t]

    def _build_pending_inputs(self, task_id: str,
                              interval: float = MESSAGE_CLAIM_INTERVAL_SECONDS
                              ) -> Callable[[], List[str]]:
        """Build a THROTTLED ``pending_inputs`` callable to pass EXPLICITLY into
        ``Orchestrator.run()``.

        Passing it explicitly (rather than leaving ``pending_inputs`` as ``None`` and letting the
        orchestrator auto-wire from a wired ``input_inbox``) is deliberate: that auto-wiring's
        ``_conv_key`` resolution (``orchestrator.py`` around lines 6839-6844) only resolves a key
        via ``quest_id`` / ``conversation_id`` / ``session_id`` / ``user_id`` in the context meta --
        a goal-only personal task (no quest_id, no chat identity in scope) would never resolve a
        key there and so would never see a mid-run message at all. Claiming straight from this
        task's own id sidesteps that: every claimed task has a task id, quest-linked or not.

        Mirrors ``_build_cancel_check``'s throttle: the orchestrator may poll this at every
        internal loop boundary (plan/gather/replan step, each deep-goal retry attempt, each
        answer-improve attempt) -- far more often than a human is plausibly sending a new message,
        so this only actually calls ``claim_task_messages`` at most once per ``interval`` seconds
        of real time and returns ``[]`` in between. A message sent inside that window is never
        lost, only picked up on the next allowed claim.

        Posts a ``status`` progress tick whenever it actually hands messages over, so the human
        sees the pickup in the task's live feed. Returns ``List[str]`` of message texts (the
        orchestrator's own ``_drain_pending`` renders these into the fold-in block). Never raises.
        """
        state = {"checked_at": 0.0}

        def _poll() -> List[str]:
            now = time.monotonic()
            if now - state["checked_at"] < interval:
                return []
            state["checked_at"] = now
            try:
                claimed = self._claim_task_messages(task_id)
                texts = self._message_texts(claimed)
            except Exception:  # noqa: BLE001 -- polling must never break the run
                return []
            if texts:
                n = len(texts)
                self._report_progress(
                    task_id, "status",
                    text=("Picked up your message." if n == 1
                          else f"Picked up your {n} messages."))
            return texts

        return _poll

    def _is_task_cancelled(self, task_id: str) -> bool:
        """Best-effort, UNTHROTTLED cancellation check for the final reporting path.

        Used right before a terminal PATCH (done/failed) and after an orchestrator error, so a run
        that dies or finishes BECAUSE it was interrupted is not mistakenly reported once more (which
        would also just 409 against a task the backend already marked cancelled). Never raises:
        ``is_task_cancelled`` is fail-open by contract, and this also tolerates a client that lacks
        the method entirely (older clients / mocks).
        """
        if not task_id:
            return False
        is_cancelled = getattr(self._client, "is_task_cancelled", None)
        if not callable(is_cancelled):
            return False
        try:
            return bool(is_cancelled(task_id))
        except Exception:  # noqa: BLE001
            return False

    def _quiet_cancelled(self, task_id: str,
                         result: Optional[OrchestratorResult] = None) -> ExecutionOutcome:
        """The quiet-cancelled path: the task was stopped mid-run (cooperatively, by the
        orchestrator's own ``cancel_check``, or detected here right before reporting).

        Do NOT PATCH the task (the backend already set ``status=cancelled``; a PATCH would just
        409) and do NOT post a done/failed message into the conversation (the backend appends its
        own terminal "cancelled" chat message) -- just a best-effort status note on the task's own
        progress stream, a log line, and a "cancelled" outcome for the poller.
        """
        self._report_progress(task_id, "status", text="Stopped: this task was cancelled.")
        log.info("task %s stopped: cancelled mid-run", task_id)
        rationale = (getattr(result, "rationale", "") or "").strip() if result else ""
        return ExecutionOutcome(task_id, "cancelled", rationale or "task was cancelled")

    def execute(self, task: Dict[str, Any], *,
                rep_preamble: Optional[str] = None) -> ExecutionOutcome:
        """Run ONE claimed task and report its outcome.

        ``rep_preamble`` (optional) is a per-task context preamble forwarded to the deep run so the
        task executes AS a specific AI rep (its persona + learned corrections). When the poller has
        resolved a rep and pulled its profile, it builds this preamble and passes it here; failing
        that, it passes the task document's own optional ``rep_preamble`` field (see
        ``Poller._task_rep_preamble``). The executor threads it straight into
        ``Orchestrator.run(rep_preamble=...)``, which forwards it to a deep runner that accepts a
        per-call ``context_preamble``, and reuses it as the voice of the fold-back done report (see
        ``_compose_done_report``). When ``None`` (any existing caller, or no rep resolved and no
        field on the task), behaviour is exactly as before.
        """
        task_id = str(task.get("id") or task.get("task_id") or "")
        # An Autopilot pass task REPLACES the normal deep-run path entirely: the pass itself is the
        # work for this task (it scans opted-in quests and creates/suggests other tasks as a side
        # effect), so it is routed BEFORE any of the context/orchestrator machinery below runs.
        if _is_autopilot_pass(task):
            return self._execute_autopilot(task, task_id)
        text = self._task_text(task)
        goal_id = task.get("goal_id")
        quest_id = task.get("quest_id")
        # If only goal_id is set, we'll need quest_id to fetch the goal. Try to infer it from
        # task metadata or fetch it separately if the backend provides it.
        if goal_id and not quest_id:
            quest_id = task.get("_inferred_quest_id")
        # conv_id links this task back to the Quest AI conversation it was delegated from. When
        # present, we post LIVE progress (started → milestones → done) INTO that chat so the
        # conversation doesn't go silent after the hand-off.
        conv_id = task.get("conv_id") or None
        # card_id (reserved, no behavior yet): forwarded on every conversation progress post so
        # a future backend can thread this task's posts under a per-idea thread.
        card_id = task.get("card_id") or None
        # model_hint: an optional per-task model/tier string stored by the consumer on the task
        # document (e.g. "opus", or any string the consumer's ModelRegistry understands).
        # Threaded into the orchestrator so the registry can honor it. None = default behavior.
        model_hint: Optional[str] = task.get("model") or None
        if not text:
            self._report_progress(task_id, "error", text="task had no instruction text to run")
            self._safe_report_failed(task_id, "task had no text/description to run")
            self._post_conv(conv_id, "I couldn't run this: the task had no instruction text.",
                            kind="failed", task_id=task_id, card_id=card_id)
            return ExecutionOutcome(task_id, "failed", "task had no text/description")

        # Announce the start: a live progress event on the task (the task-detail stream) AND, when a
        # conv_id links this task to a chat, a started message into that chat.
        self._report_progress(task_id, "started", text=f"Started working on this: {text}")
        self._post_conv(conv_id, f"Started working on this: {text}", kind="started",
                        task_id=task_id, card_id=card_id)

        # ONE initial drain: claim any messages the human sent while this task sat queued (e.g. a
        # steer sent right after delegating, before a background lane picked it up) and fold them
        # into the FIRST prompt the orchestrator sees, so they land on this run's opening attempt
        # instead of waiting for a later drain point. The backend's claim endpoint already posts
        # its own ``message_ack`` progress tick when it hands these over; this status tick is
        # separate -- it names that the FOLD happened, for the live feed.
        _initial_texts = self._message_texts(self._claim_task_messages(task_id))
        if _initial_texts:
            # Reuse the orchestrator's own rendering (a one-shot callable over the already-claimed
            # texts) so this fold-in reads IDENTICAL to one folded in mid-run by the orchestrator's
            # ``_drain_pending`` (deep retry loop, answer-improve loop, planner loop) -- one voice
            # for "here is what the user said since you started", regardless of which drain point
            # picked it up.
            _folded_block = Orchestrator._drain_pending(lambda: _initial_texts)
            if _folded_block:
                text = f"{text}\n\n{_folded_block}"
            _n = len(_initial_texts)
            self._report_progress(
                task_id, "status",
                text=("Picked up your message that arrived before this started."
                      if _n == 1 else
                      f"Picked up your {_n} messages that arrived before this started."))

        # Fetch goal + quest context + conversation history from Quest API if available, and build
        # a context_view for the orchestrator so the deep agent knows what goal/quest it's working on
        # and the prior conversation that led to the task.
        context_view = self._build_context_view(
            goal_id, quest_id, conv_id, rep_id=task.get("assignee_rep_id"))

        # Route all orchestrator events (except raw streaming partials) to the task's live progress
        # stream so the task-detail SSE shows step-by-step what the AI is doing (plan, read, replan,
        # tokens). Milestones additionally post into the originating chat (same as MilestoneSink).
        sink = _TaskProgressSink(
            task_id,
            self._report_progress,
            on_milestone=lambda ev: self._on_milestone(task_id, conv_id, ev,
                                                       card_id=card_id),
        )

        # Build a scope for finding RELATED past conversations (the orchestrator's Step 1, User
        # Input Understanding) from whatever identity the task carries. Omit missing keys so a
        # store's best-effort scope filter only constrains on fields actually present.
        conv_scope: Dict[str, Any] = {}
        for _src, _dst in (("user_id", "user_id"), ("team_id", "team_id"),
                           ("team_ids", "team_ids"), ("participant_id", "participant_id")):
            _val = task.get(_src)
            if _val is not None:
                conv_scope[_dst] = _val

        # Thread the task's goal identity into the orchestrator's context meta. quest_id already
        # travels as its own run() param, but a personal "goal is the hub" task carries its id in
        # goal_id (often with NO quest_id), and context assemblers that scope by goal — e.g.
        # FileContextStore's quest_folder_map boost — would otherwise never see it.
        context_meta: Optional[Dict[str, Any]] = {"goal_id": goal_id} if goal_id else None

        # Cooperative mid-run cancellation: a THROTTLED check (see _build_cancel_check) threaded
        # into the orchestrator so a human hitting "stop" while this task is in_progress can abort
        # the run cleanly at its next loop boundary instead of running to completion regardless.
        cancel_check = self._build_cancel_check(task_id)

        # Explicit mid-run steering channel: a THROTTLED callable (see _build_pending_inputs) that
        # claims any NEW messages the human sends while this task is in_progress, so the deep retry
        # loop / answer-improve loop / outer planner loop can fold them in at their next drain
        # point. Passed EXPLICITLY rather than left as None -- see _build_pending_inputs' docstring
        # for why relying on Orchestrator.run's own input_inbox auto-wiring would silently miss a
        # goal-only personal task (its _conv_key resolution only resolves via quest_id today).
        pending_inputs = self._build_pending_inputs(task_id)

        # Per-task working directory: when this task's goal/quest resolves through the configured
        # quest_folder_map, the deep run starts in THAT folder (its synced quest folder) for this
        # run only, instead of the deep-runner's configured global working_dir. Applies to every
        # task, not just autopilot-created ones (see quest_autopilot_design.md's execution-
        # environment section).
        working_dir_override = self._resolve_working_dir(goal_id, quest_id)

        try:
            result: OrchestratorResult = self._orch.run(
                text, quest_id=quest_id, context_view=context_view, mode=Mode.BACKGROUND,
                sink=sink, model_hint=model_hint, rep_preamble=rep_preamble,
                context_meta=context_meta, working_dir_override=working_dir_override,
                conv_id=conv_id, conv_scope=conv_scope or None, cancel_check=cancel_check,
                pending_inputs=pending_inputs)
        except Exception as e:  # noqa: BLE001 — brain failure -> failed report, never crash poller
            # A run that raises BECAUSE it was interrupted must not be reported as failed: check
            # (unthrottled, this is the terminal path) whether the task was cancelled meanwhile.
            if self._is_task_cancelled(task_id):
                return self._quiet_cancelled(task_id)
            msg = f"orchestrator error: {type(e).__name__}: {e}"
            self._report_progress(task_id, "error", text=msg)
            self._safe_report_failed(task_id, msg)
            self._post_conv(conv_id, f"I hit an error working on this: {msg}", kind="failed",
                            task_id=task_id, card_id=card_id)
            return ExecutionOutcome(task_id, "failed", msg)

        return self._report(task_id, result, conv_id, request_text=text,
                            rep_preamble=rep_preamble,
                            card_id=(task.get("card_id") or None))

    def report(self, task_id: str, result: OrchestratorResult,
               conv_id: Optional[str] = None, *,
               request_text: Optional[str] = None,
               rep_preamble: Optional[str] = None,
               card_id: Optional[str] = None) -> ExecutionOutcome:
        """Public: map an ALREADY-PRODUCED OrchestratorResult onto the Quest callback + chat.

        ``execute()`` runs the brain and then reports; but an integrator whose deep run executes
        ASYNCHRONOUSLY *outside* ``execute()`` (e.g. a host application spawns a ``/goal`` subprocess
        and only learns the outcome later, on a different thread) needs to report that finished
        outcome through the SAME three-way policy — done / needs_you / failed — and the same
        post-back-into-chat behaviour. They build the OrchestratorResult from the finished run and
        call ``report(...)`` so async and in-loop runs report IDENTICALLY. Thin, deliberate
        wrapper over the internal ``_report`` so the policy lives in exactly one place.

        ``request_text`` (optional) is the task's original instruction text; when given, a fully
        met deep result's done message is folded through the report synthesis + claim check (see
        ``_compose_done_report``). ``rep_preamble`` rides into that synthesis. ``card_id``
        (optional, reserved) is stamped on the conversation progress posts."""
        return self._report(task_id, result, conv_id, request_text=request_text,
                            rep_preamble=rep_preamble, card_id=card_id)

    def _on_milestone(self, task_id: str, conv_id: Optional[str], event: ProgressEvent,
                      card_id: Optional[str] = None) -> None:
        """Surface a real milestone: the live task-detail stream AND the originating chat.

        Background runs surface only real milestones/decisions/results (the MilestoneSink policy),
        so this fires for genuine progress — never planning/reading chatter. We fan each milestone
        to BOTH the task progress stream (kind="exec") and the chat (kind="progress"). Both posts
        are best-effort: a dropped progress event must never affect the task outcome."""
        if event.text:
            self._report_progress(task_id, "exec", text=event.text)
            self._post_conv(conv_id, event.text, kind="progress", task_id=task_id,
                            card_id=card_id)

    def _report_progress(self, task_id: str, kind: str, *, text: Optional[str] = None,
                         output: Optional[str] = None,
                         data: Optional[Dict[str, Any]] = None) -> None:
        """Best-effort: post a live execution-progress event onto the task (the task-detail stream).

        No-ops when the client lacks ``report_progress`` (older clients / mocks), and never raises —
        the client's own ``report_progress`` is best-effort, but we also guard the call here so a
        progress event can never affect the task's success/failure."""
        if not task_id:
            return
        report = getattr(self._client, "report_progress", None)
        if callable(report):
            self._safe(lambda _d=data: report(task_id, kind, text=text, output=output, data=_d))

    def _post_conv(self, conv_id: Optional[str], content: str, *, kind: str,
                   task_id: Optional[str] = None,
                   card_id: Optional[str] = None) -> None:
        """Best-effort: append a live progress message into the originating chat, if one is linked.

        ``task_id``, when given, is stamped on the posted message so the frontend can correlate it
        back to the task's own lifecycle. ``card_id`` (reserved, no behavior yet) rides on the
        progress-post body when the task carries one, for future per-idea threading; it is only
        forwarded when set, so clients without the parameter are untouched. Never raises and never
        affects the task's success/failure: if the conversation post fails (network, conversation
        gone), the task still reports its result normally via PATCH."""
        if not conv_id or not content:
            return
        post = getattr(self._client, "post_conversation_message", None)
        if callable(post):
            extra = {"card_id": card_id} if card_id else {}
            self._safe(lambda: post(conv_id, content, kind=kind, task_id=task_id, **extra))

    def _build_context_view(self, goal_id: Optional[str], quest_id: Optional[str],
                            conv_id: Optional[str] = None,
                            rep_id: Optional[str] = None) -> str:
        """Fetch goal + quest metadata + notes + conversation history from the Quest API.

        The context_view is passed to the orchestrator so the deep agent knows what goal/quest
        it's working on, what progress has been made, and the prior conversation that led to
        the task. Gracefully handles missing API (no-ops when client lacks needed methods) and
        API errors (builds partial context)."""
        parts = []

        # Fetch prior conversation history if this task was delegated from a chat — but ONLY as a
        # FALLBACK when the orchestrator has no ConversationStore wired. When a store IS wired, the
        # orchestrator's Step 1 (User Input Understanding) pulls the relevant slice itself (and
        # resolves the request from it), so we must not also dump the full transcript here.
        if (conv_id and self._retrieval
                and getattr(self._orch, "conversation_store", None) is None):
            try:
                # Try to read the EXACT conversation by its conv_id. BOUNDED: max_bytes caps the
                # transcript at the source, so a long conversation can never grow this task's
                # prompt without bound (a conversation-aware adapter keeps the recent tail).
                obs = self._retrieval.read_section(str(conv_id),
                                                   max_bytes=CONV_CONTEXT_MAX_BYTES)
                if obs and obs.kind == "read" and obs.text:
                    # Explicitly mark which conversation we loaded to disambiguate from previous tasks
                    parts.append(f"=== Prior Conversation Context (conv_id={conv_id}) ===\n{obs.text}\n")
                elif obs and obs.kind == "error":
                    # Conversation not found is non-critical, but log it for debugging
                    # (the task still runs with just the goal/quest context, not the prior chat)
                    pass
            except Exception:  # noqa: BLE001 — conversation fetch failure is non-critical
                pass

        if not goal_id and not quest_id:
            parts.append(KEEP_GOING_CONTRACT)
            return "\n".join(parts)
        # Not inside the ``if quest_id`` below: a task created against a quest carries that
        # quest's id in goal_id and leaves quest_id null, and those are exactly the runs that mail.
        self._append_email_contract(parts, goal_id, quest_id, rep_id)
        # Fetch quest metadata if available
        if quest_id:
            get_quest = getattr(self._client, "get_quest", None)
            if callable(get_quest):
                try:
                    quest = get_quest(quest_id)
                    if quest:
                        outcome = quest.get("outcome", "")
                        if outcome:
                            parts.append(f"Quest outcome: {outcome}")
                        completed = quest.get("completed")
                        if completed is not None:
                            status = "completed" if completed else "in progress"
                            parts.append(f"Quest status: {status}")
                except Exception:  # noqa: BLE001
                    pass  # API unavailable or error; continue with what we have

        # Fetch goal metadata if available
        if goal_id and quest_id:
            get_goal = getattr(self._client, "get_goal", None)
            if callable(get_goal):
                try:
                    goal = get_goal(goal_id, quest_id=quest_id)
                    if goal:
                        name = goal.get("name", "")
                        if name:
                            parts.append(f"Goal: {name}")
                        description = goal.get("description", "")
                        if description:
                            parts.append(f"Goal description: {description}")
                        deadline = goal.get("deadline", "")
                        if deadline:
                            parts.append(f"Goal deadline: {deadline}")
                        completed = goal.get("completed")
                        if completed is not None:
                            status = "completed" if completed else "in progress"
                            parts.append(f"Goal status: {status}")
                except Exception:  # noqa: BLE001
                    pass  # API unavailable or error; continue with what we have

        # The notes on the QUEST are the person's reply channel, so they are fetched whenever there
        # is a quest — with or without a goal on the task.
        if quest_id:
            notes_text = render_goal_notes(self._fetch_person_notes(quest_id, goal_id))
            if notes_text:
                parts.append(notes_text)

            # What the person captured on their phone and has not acted on yet. Autopilot passes
            # already read these; an ordinary scheduled run did not, so the same capture steered a
            # pass and was invisible to the daily task working the very quest it was about.
            insights_text = self._fetch_person_captures()
            if insights_text:
                parts.append(insights_text)

            history_text = render_run_history(self._fetch_run_history(quest_id))
            if history_text:
                parts.append(history_text)
                # Only where there IS a "previously". On a quest nothing has run on yet, nobody has
                # been asked to do anything, so this would only make a first run hedge about work
                # that was never requested.
                parts.append(NO_ASSUMED_PROGRESS_CONTRACT)

            if notes_text or insights_text:
                parts.append(REPLY_LOOP_CONTRACT)

        # Applies to every run: hitting a blocker is normal, stopping because of one is not.
        parts.append(KEEP_GOING_CONTRACT)
        return "\n".join(parts)  # Combined conversation + quest/goal context

    def _append_email_contract(self, parts: List[str], goal_id: Optional[str],
                               quest_id: Optional[str],
                               rep_id: Optional[str] = None) -> None:
        """Add the email contract when (and only when) this quest actually mails its work.

        Both ids are tried, quest_id first and goal_id second, because a task created against a
        quest carries that quest's id in ``goal_id`` and leaves ``quest_id`` NULL -- the same
        goal-or-quest ambiguity the poller already handles for the quest folder map. Reading only
        ``quest_id`` meant the contract never fired for those tasks: every run concluded email was
        off, mailed by hand through a local script, and the person received the automatic copy and
        the hand-rolled one, minutes apart -- the exact duplicate this contract exists to prevent.
        A ``get_quest`` on an id that is a goal rather than a quest returns {} and falls through,
        so trying both is safe as well as necessary.
        """
        get_quest = getattr(self._client, "get_quest", None)
        if not callable(get_quest):
            return
        for candidate in (quest_id, goal_id):
            if not candidate:
                continue
            try:
                quest = get_quest(candidate) or {}
            except Exception:  # noqa: BLE001 — never let a context extra break a run
                continue
            settings = ((quest.get("autopilot") or {}).get("email") or {})
            if settings.get("enabled"):
                # The id that actually carries the settings, so the printed command names the
                # quest whose recipients the mail will go to.
                parts.append(email_contract(candidate, rep_id))
                return

    def _fetch_person_notes(self, quest_id: str, goal_id: Optional[str]) -> List[Dict[str, Any]]:
        """The quest's notes, which is where a person answers their AI.

        ``list_quest_notes`` (GET /api/quests/{id}/notes) is the collection that actually holds
        them: it is what the Quest app writes, what ``quest_folder_sync`` mirrors, and what an
        owner adds a note to. ``list_goal_notes`` is tried only as a fallback for a backend that
        implements the per-goal route -- the reference backend does not, so asking it first (as
        this did) returned a 404 and left every run with NO notes at all while the person's replies
        sat unread on the quest.
        """
        list_quest_notes = getattr(self._client, "list_quest_notes", None)
        if callable(list_quest_notes):
            try:
                notes = list(list_quest_notes(quest_id) or [])
                if notes:
                    return notes
            except Exception:  # noqa: BLE001
                pass  # API unavailable or error; fall through to the per-goal route
        list_goal_notes = getattr(self._client, "list_goal_notes", None)
        if goal_id and callable(list_goal_notes):
            try:
                return list(list_goal_notes(goal_id, quest_id=quest_id,
                                            limit=NOTE_CONTEXT_LIMIT) or [])
            except Exception:  # noqa: BLE001
                pass  # API unavailable or error; continue with what we have
        return []

    def _fetch_run_history(self, quest_id: str) -> List[Dict[str, Any]]:
        """Recent tasks on this quest, whatever their outcome.

        Deliberately unfiltered by status: the run that most needs reading is often the one that
        FAILED, because its work happened anyway and only its confirmation did not.
        """
        list_tasks = getattr(self._client, "list_tasks", None)
        if not callable(list_tasks):
            return []
        try:
            return list(list_tasks(goal_id=quest_id) or [])
        except Exception:  # noqa: BLE001 — history is context, never a reason to fail a run
            return []

    def _fetch_person_captures(self) -> str:
        """The person's recent unacted captures, rendered, or "" when there are none."""
        try:
            from .insights import collect_unacted_insights
            return collect_unacted_insights(self._client).as_text()
        except Exception:  # noqa: BLE001
            return ""  # never let a context extra break a run

    # --- result -> Quest callback -------------------------------------------

    def _report(self, task_id: str, result: OrchestratorResult,
                conv_id: Optional[str] = None, *,
                request_text: Optional[str] = None,
                rep_preamble: Optional[str] = None,
                card_id: Optional[str] = None) -> ExecutionOutcome:
        # Cooperative cancellation: ``result.kind == "cancelled"`` is the orchestrator's OWN
        # cooperative signal (its ``cancel_check`` returned True mid-run); the extra
        # ``_is_task_cancelled`` re-check covers the race where the run finished (or an async
        # caller reports through the public ``report()`` API) right as/after a human cancelled the
        # task, before we PATCH a terminal status that would just 409 anyway.
        if result.kind == "cancelled" or self._is_task_cancelled(task_id):
            return self._quiet_cancelled(task_id, result)
        if result.kind == "answer":
            text = result.text or "(no answer produced)"
            # BROKEN-PROMISE GUARD: if the orchestrator rewrote this answer to be honest about a
            # claimed action that did NOT actually complete (claim_corrected), the task is NOT done
            # — surface it as needs_you so a human picks it up, rather than marking it complete on a
            # reply that says the work was not finished. (A plain ``partial`` best-effort answer,
            # from the read-budget cap, is still a legitimate informational answer and stays done.)
            if getattr(result, "claim_corrected", False):
                self._report_progress(task_id, "done", text="Paused. Needs you.", output=text)
                self._safe(lambda: self._client.report_needs_you(task_id, text, ""))
                self._post_conv(conv_id, text, kind="needs_you", task_id=task_id, card_id=card_id)
                return ExecutionOutcome(task_id, "needs_you", text)
            # Append goal-verdict reasoning so the reader knows whether the goal was confirmed
            # met, hit max iterations unverified, or was a best-effort partial answer.
            verdict_suffix = ""
            exit_reason = getattr(result, "exit_reason", "")
            goal_verdict = getattr(result, "goal_verdict", None)
            if exit_reason == "max_turns" and goal_verdict:
                reason = (goal_verdict.get("reason") or "").strip()
                next_action = (goal_verdict.get("next_action") or "").strip()
                verdict_suffix = f"\n\n---\nGoal not fully verified after all attempts."
                if reason:
                    verdict_suffix += f" {reason}"
                if next_action:
                    verdict_suffix += f" To complete: {next_action}"
            elif exit_reason == "read_budget":
                verdict_suffix = "\n\n---\nNote: this is a best-effort answer based on context gathered so far."
            done_text = text + verdict_suffix if verdict_suffix else text
            self._report_progress(task_id, "done", text="Done.", output=done_text)
            self._safe(lambda: self._client.report_done(task_id, done_text))
            self._post_conv(conv_id, done_text, kind="done", task_id=task_id, card_id=card_id)
            return ExecutionOutcome(task_id, "done", done_text)

        if result.kind == "confirm":
            summary = result.question or "A human decision is required before proceeding."
            # needs_you is a terminal-but-paused state; close the live stream with a 'done' tick
            # noting it now needs a human, so the stream doesn't hang open.
            self._report_progress(task_id, "done", text=f"Paused, needs you: {summary}")
            self._post_conv(conv_id, summary, kind="decision", task_id=task_id, card_id=card_id)
            if result.decision_id:
                self._safe(lambda: self._client.report_needs_you(task_id, summary, result.decision_id))
                return ExecutionOutcome(task_id, "needs_you", summary, result.decision_id)
            # No decision id (no escalation sink wired) — surface as needs_you without an id.
            self._safe(lambda: self._client.report_needs_you(task_id, summary, ""))
            return ExecutionOutcome(task_id, "needs_you", summary)

        # deep
        deep = result.deep_results
        if deep and all(d.met for d in deep):
            summary = "\n\n".join(d.output for d in deep if d.output) or "Goal(s) met."
            # FOLD-BACK: the raw deep output is a worker transcript tail; the done post into the
            # chat should read as the AI reporting its own finished work. The synthesized report
            # is CLAIM-CHECKED against the run's execution record before it replaces the raw
            # summary; any doubt keeps the raw (already goal-verified) output.
            done_report = self._compose_done_report(request_text, summary, result, rep_preamble)
            self._report_progress(task_id, "done", text="Done.", output=done_report)
            self._safe(lambda: self._client.report_done(task_id, done_report))
            self._post_conv(conv_id, done_report, kind="done", task_id=task_id,
                            card_id=card_id)
            return ExecutionOutcome(task_id, "done", done_report)
        # A deep run that raised a human decision instead of finishing.
        decision_id = next((d.decision_id for d in deep if d.decision_id), None)
        if decision_id:
            summary = "A human decision is required to finish this task."
            # A confirm-before-act run carries the prepared output (e.g. the code awaiting review).
            chat_text = next((d.output for d in deep if d.output), None) or summary
            self._report_progress(task_id, "done", text=f"Paused, needs you: {summary}")
            self._safe(lambda: self._client.report_needs_you(task_id, summary, decision_id))
            self._post_conv(conv_id, chat_text, kind="decision", task_id=task_id,
                            card_id=card_id)
            return ExecutionOutcome(task_id, "needs_you", summary, decision_id)
        # UNVERIFIED is never reported as done, and never as a bare failure either: the work RAN
        # but its verification could not (LLM outage, no verify tier, parse failure), so the
        # outcome is genuinely unknown. The chat message must say that plainly, presenting any
        # work output as unconfirmed, and the task stays non-done (failed) so a human checks it.
        unverified = [d for d in deep if not d.met and (d.error or "").startswith("Unverified")]
        if unverified:
            reasons = "; ".join((d.error or "").strip() for d in unverified)
            work = "\n\n".join(d.output for d in deep if d.output).strip()
            disclosure = ("Important: this work ran but I could NOT verify the result as "
                          f"complete ({reasons}). Treat it as unconfirmed until someone checks "
                          "it. It is not marked done.")
            msg = (work + "\n\n" + disclosure) if work else disclosure
            self._report_progress(task_id, "error", text=disclosure)
            self._safe(lambda: self._client.report_failed(task_id, msg))
            self._post_conv(conv_id, msg, kind="failed", task_id=task_id, card_id=card_id)
            return ExecutionOutcome(task_id, "failed", msg)
        # Otherwise the run hit a limit / errored.
        errs = "; ".join(d.error for d in deep if d.error) or "the goal was not met"
        if not deep:                 # deep requested but no runner wired -> needs human/runner
            errs = "deep work required but no deep-runner is configured: " + "; ".join(result.goals)
        self._report_progress(task_id, "error", text=errs)
        self._safe(lambda: self._client.report_failed(task_id, errs))
        self._post_conv(conv_id, f"I couldn't complete this: {errs}", kind="failed", task_id=task_id,
                        card_id=card_id)
        return ExecutionOutcome(task_id, "failed", errs)

    def _compose_done_report(self, request_text: Optional[str], raw_summary: str,
                             result: OrchestratorResult,
                             rep_preamble: Optional[str] = None) -> str:
        """Fold a fully met deep run's raw output into the done message posted back to the user.

        Uses the orchestrator's report synthesis (same prompt shape as the interactive after-deep
        fold-back, worker tier) so the message reads as the AI reporting its own finished work,
        then CLAIM-CHECKS the rewrite against the run's execution record (claims_unexecuted): a
        rewrite that claims work the record does not back, or that cannot be checked at all, is
        discarded for the raw summary, which is the goal-verified output itself. Never raises and
        never loses the substance: any failure returns ``raw_summary`` unchanged.
        """
        request = (request_text or "").strip()
        if not request or not (raw_summary or "").strip():
            return raw_summary
        synthesize = getattr(self._orch, "synthesize_task_report", None)
        claim_check = getattr(self._orch, "report_claims_unbacked", None)
        if not callable(synthesize) or not callable(claim_check):
            return raw_summary        # older/stub brains: keep the raw verified output
        try:
            synthesized = (synthesize(request, raw_summary,
                                      rep_preamble=rep_preamble) or "").strip()
            if not synthesized or synthesized == raw_summary:
                return raw_summary
            unbacked = claim_check(request, synthesized,
                                   getattr(result, "execution_record", None))
            if unbacked is False:
                return synthesized
            # True (unbacked claim) or None (check could not run): never post an unchecked
            # rewrite; the raw summary is the output the goal loop already verified.
            log.info("done-report rewrite discarded (claim check: %s); posting raw output",
                     "unbacked claim" if unbacked else "not checkable")
        except Exception:  # noqa: BLE001 — report composition must never affect the outcome
            log.warning("done-report composition failed; posting raw output", exc_info=True)
        return raw_summary

    # --- per-task working directory (quest_folder_map) ------------------------

    def _resolve_working_dir(self, goal_id: Optional[str],
                             quest_id: Optional[str]) -> Optional[str]:
        """Resolve this task's deep-run working-directory override from ``quest_folder_map``.

        Mirrors the poller's own ``_quest_folder_for`` precedence (goal_id first, then quest_id) so
        a task tied to a synced quest folder starts the deep agent THERE instead of the deep-
        runner's configured global working_dir. Returns None (fallback = the global default)
        when unconfigured or the task's ids aren't in the map."""
        if not self._quest_folder_map:
            return None
        qid = goal_id or quest_id
        if not qid:
            return None
        return self._quest_folder_map.get(str(qid)) or None

    # --- autopilot pass routing (handler == "autopilot") ----------------------

    def _execute_autopilot(self, task: Dict[str, Any], task_id: str) -> ExecutionOutcome:
        """Run the wired ``AutopilotPass`` for a ``handler == "autopilot"`` task and report its
        outcome through the normal progress/report calls (a plain 'done' unless the pass itself
        cannot run at all). ``AutopilotPass.run`` already isolates each quest's own failures into
        the result (see ``runner.autopilot``), so nothing here needs a per-quest try/except; this
        method's own try/except only guards against the pass failing to run at all."""
        self._report_progress(task_id, "started", text="Started an autopilot pass.")
        if self._autopilot is None:
            msg = "autopilot is not configured on this runner (no AutopilotPass wired)"
            self._report_progress(task_id, "error", text=msg)
            self._safe_report_failed(task_id, msg)
            return ExecutionOutcome(task_id, "failed", msg)
        try:
            result = self._autopilot.run(task)
            summary = result.summary_text()
            self._report_progress(task_id, "done", text="Done.", output=summary)
            self._safe(lambda: self._client.report_done(task_id, summary))
            return ExecutionOutcome(task_id, "done", summary)
        except Exception as e:  # noqa: BLE001 — the pass itself must never crash the poller
            msg = f"autopilot pass error: {type(e).__name__}: {e}"
            self._report_progress(task_id, "error", text=msg)
            self._safe_report_failed(task_id, msg)
            return ExecutionOutcome(task_id, "failed", msg)

    # --- safety wrappers (reporting must not crash the poller) ---------------

    def _safe(self, fn):
        try:
            fn()
        except Exception:  # noqa: BLE001
            log.error("report failed", exc_info=True)

    def _safe_report_failed(self, task_id: str, msg: str):
        self._safe(lambda: self._client.report_failed(task_id, msg))
