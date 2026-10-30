# The stream-json deep runner (steer, stop, pause)

`StreamJsonGoalRunner` is a **third** `DeepRunner` implementation of the solution `run-deep-work`,
declared with `@implementation("run-deep-work", "stream_json")` next to the one-shot
`SubprocessGoalRunner`. It keeps one live Claude Code session open for the life of a run, so a
person can add direction, stop or pause while the worker is busy, like typing into an interactive
Claude Code.

**It is an alternative, not the default.** `SubprocessGoalRunner` stays the selected implementation
until the metric `metric-receive-input-mid-run` shows this one wins on real runs. Select it for a
lane with `ESOI_SELECT_QUEST_AI_RUNNER_RUN_DEEP_WORK=stream_json` or `[selection]` in `esoi.toml`
(see [evolvable-structure.md](evolvable-structure.md)).

## What it does

It runs `claude -p --input-format stream-json --output-format stream-json --verbose
--replay-user-messages`, writes the prompt as the first user line, and then drives the session from
one loop thread.

| Action | What happens |
|---|---|
| Steer | While the run is active the loop asks its `RunControl` for new messages every `poll_seconds` (2 s from the task executor). Each message is written to stdin as a user line. Claude Code reads it in the same turn, as soon as the tool it is running finishes. |
| Seen | Claude Code replays each user message it takes. When the replay of a steering message comes back, the runner calls `RunControl.mark_seen(ids, "deep_worker")`. "Seen" means the model received it, not that the runner held it. |
| Stop | The loop sends an `interrupt` control request. The running step is cut off, finished work stays, and the session is kept. The process is then released: stdin is closed, and a worker that does not exit is sent SIGTERM and then SIGKILL after `cancel_grace_seconds` each. The result is `cancelled` with the session id. |
| Pause | The same interrupt, but the result is `paused` (and `cancelled`, so the orchestrator stops its loop quietly) with the session id. Nothing stays running. A later `run_goal(resume_session_id=...)` resumes the same Claude Code session with `--resume`, on the same task thread. |
| Resume | `--resume <session id>`, the path the one-shot runner already uses, including the retry from a cold start when the session is gone. |

It keeps what the one-shot runner already does: the live progress stream (the session JSONL monitor
tails the same file, so `exec` ticks look the same), `--resume`, the subscription login (the same
environment, no API key), the usage-limit wait, the empty-output guard, the liveness review and the
optional wall-clock cap.

## The control channel

`hubofhubs.ports.control` defines `RunControl`, handed to one call of `run_goal(..., control=...)`:

| Field | Meaning |
|---|---|
| `claim_inputs()` | Messages sent since the last call, each handed over once. |
| `mark_seen(ids, where)` | The model received those messages. |
| `cancel_requested()` / `pause_requested()` | Polled while the run works. |
| `poll_seconds` | How often to look. |

`Orchestrator.run(..., run_control=...)` passes it to a worker whose `run_goal` **names** a `control`
keyword (a worker that only accepts `**kwargs` is not given it, because a claim nobody reads must
never happen), and only on a single-goal run. The subgoals of a fan-out run at the same time, and
one control handed to several workers could deliver a message to the wrong one. The task executor
builds a control bound to its own task id (`TaskExecutor._build_run_control`): claims go to
`POST /api/assistant-tasks/{id}/messages/claim`, seen to `.../messages/seen`, a stop is read the
way a cancel is, and a pause from `QuestClient.is_task_pause_requested` (the task's
`pause_requested` field, which a backend that has no such field reads as false).

## A paused task: the hand-off to the queue

A run paused through the control ends as `DeepResult(paused=True, cancelled=True)` carrying its
session id, and the orchestrator returns `kind="cancelled"` with those `deep_results`. The task
executor tells a pause from a cancel by `DeepResult.paused` (structural, never by text) and hands the
task back the way `requeue_for_usage_limit` puts one back, without its timing:

1. It does not call the verifier, report done, failed, needs_you or incomplete, post into the chat,
   or claim any message.
2. `QuestClient.requeue_paused_task` PATCHes `{"status": "queued", "resume_session_id": <session>}`.
   The backend keeps `pause_requested` on the task, so a claim PATCH answers 409 and discovery skips
   it until `POST /api/assistant-tasks/{id}/resume` clears the flag. A refused write returns `None`;
   the executor then reports the task failed and says why, because it would otherwise stay
   in_progress with nothing to pick it up. A task cancelled while it was stopping stays cancelled.
3. The task feed gets one status line starting `Paused.`, and the executor returns outcome status
   `paused` (the poller records no rep turn for it, as for a usage-limit wait).
4. A lane that claims the resumed task reads `resume_session_id` and the deep runner resumes that
   same Claude Code session with `--resume`, on the same thread.

Messages are not lost. A message steered in after the pause stays unclaimed (the pause path claims
nothing) and the resumed run's opening drain delivers it. One the run had claimed but never
confirmed reaching the model (`DeepResult.unseen_inputs`) is posted back onto the task with
`QuestClient.post_task_message` (which raises when refused), and is quoted on the feed if even that
write fails. Before this hand-off existed a paused result was read as a cancel: the feed said "Stopped:
this task was cancelled.", nothing was PATCHed, the session id was dropped and the task stayed
in_progress.

## Guarantees and how they hold

* **Exactly once, only to its own run.** The control belongs to one call, so a run can only claim
  its own task's messages. One loop thread claims, writes and decides that the run is over, so a
  message cannot be claimed after that decision. A message that is not yet claimed when the run
  finishes stays with the caller, which opens a follow-up run for it (the leftover path in the
  executor).
* **A message that lands as the run finishes.** If it is written just before the model's last
  result, the loop sees a written message whose replay has not come back, so it does not finish: the
  message starts one more turn, is replayed, and the run ends after that turn. If it is not claimed
  yet, it is left for the follow-up run. It is never both.
* **None lost.** Every claimed message is recorded before it is written. One whose replay never
  comes (the process died, a stop landed first, an old CLI ignores the flag) is returned in
  `DeepResult.unseen_inputs`, so the caller can show it or send it with the resume.
* **Work stays.** An interrupt cuts off the running tool only. Steps that finished are in the
  session, and Claude Code tells the model on resume that a cut-off call may or may not have taken
  effect. A stop is not a rollback.

## Requirements and limits

* **Claude Code 2.1.219 or newer** (the release that answers an interrupt with a receipt). An older
  or unreadable version makes the runner hand the goal to the one-shot runner instead of pretending.
* **Priority:** only the default priority is used. In a probe on this machine, `priority: "now"` did
  not move a running foreground command to the background as the documentation says, so feedback is
  never sent with it.
* **Replay matching:** a steering message is marked seen when a replayed user message equals its
  text (or contains it, if several were merged into one turn). The format of the replay was
  observed on 2.1.303; the live ladder in Phase 4 of the real-time design should confirm it.
* **Cost accounting:** tokens and dollars are taken from the last `result` event of the session,
  read as cumulative. The live ladder should confirm that too.
* **One long-lived process per active run** on a shared host. A paused run holds no process.

## In the interactive terminal

The Textual terminal (`quest-ai-runner chat`) steers, stops and pauses a running deep worker the way
typing into interactive Claude Code does. Turn it on for a session with the one variable that
selects the runner, set before starting the chat:

```bash
ESOI_SELECT_QUEST_AI_RUNNER_RUN_DEEP_WORK=stream_json quest-ai-runner chat
```

(The same selection as `[selection]` in `esoi.toml`; the default stays `subprocess`. `core.resolve.chosen_name("run-deep-work")`
prints which one a process would use.) With the one-shot runner nothing below applies and the
terminal behaves as before: a message typed while a deep run works is queued, the orchestrator's own
retry loop or the end-of-turn flush picks it up.

| You do | What happens |
|---|---|
| Type a message while a live worker runs | It is queued as usual (`up queued (N)`), then the worker takes it within a second (`TerminalRunControl`, `textual_ui_control.py`), writes it into the session and the transcript says `sent to the running worker`, then `The worker received 1 message` once the model replays it. |
| Esc | Stops the running step and keeps the session. Finished work stays. `/resume` continues the same session. |
| `/pause` | The same interrupt, ending the run `paused`. Messages typed since are held and go with `/resume`. `/pause` says so when no live worker exists (planning, or the one-shot runner). |
| `/resume` | Starts a deep run of the paused or stopped goal straight away, with `--resume <session>`, plus any held messages. The planner is not asked (`Orchestrator.run_stream(deep_goal=..., resume_session_id=...)`), so nothing it could have chosen, such as a plain answer or a question, can leave the session unresumed. |

A message is delivered exactly once. The live worker, the orchestrator's own drains and the
end-of-turn flush all draw from the same inbox, and a draw is atomic. A message the worker took but
never confirmed (`DeepResult.unseen_inputs`, or a claim with no `mark_seen`) goes back to the front
of the inbox when the turn ends, so the end-of-turn flush sends it again, and a transcript line says
so. After a pause it is held for `/resume` instead.

## Tests

`tests/test_goal_runner_stream.py` drives the real runner against `tests/fakes/fake_claude_stream.py`,
a scripted stand-in that speaks the protocol (no model, no cost): steering while a tool runs,
stop keeping the session, pause then resume, an interrupt that is ignored (terminate, then kill), a
message landing as the run finishes, a message after the run ended, two concurrent runs with no
cross delivery, a message never replayed (returned as unseen), and an old CLI falling back to the
one-shot runner. `tests/test_run_control_plumbing.py` pins how the control reaches (and does not
reach) a worker and how the executor binds it to its task. `tests/test_executor_pause.py` pins the
pause hand-off above (including a full pause then resume against the scripted worker).
`tests/test_terminal_run_control.py` drives
the terminal's control against the real runner (steer, stop, pause then `--resume`, an unconfirmed
message put back first) and pins `/pause`, `/resume` and the end of a turn.
