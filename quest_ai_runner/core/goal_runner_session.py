"""Reading the worker's own session record: live monitoring, resume lookups, and end-of-run diagnostics.

Split out of ``goal_runner.py`` by definition; that module re-exports every name here.
"""
from __future__ import annotations

import json
import re
import threading
import time
from typing import Callable, Dict, List, Optional, Set
from pathlib import Path

from .adapters_events import EVENT_EXEC, EVENT_STATUS
from .adapters_values import ProgressEvent
from .runtime_settings import core_settings
from .goal_runner_contract import DEFAULT_MONITOR_HEARTBEAT_SECONDS, _log


def _claude_project_bases(working_dir: Optional[str] = None) -> List[Path]:
    """Every ``projects`` directory a Claude Code session could be written under, best first.

    Claude Code writes sessions to ``$CLAUDE_CONFIG_DIR/projects`` (``~/.claude/projects`` when
    unset), so that location leads. The working dir's own ``.claude/projects`` and the home one
    follow for lanes that run Claude Code with a different config dir than the runner's.
    """
    bases: List[Path] = []
    config_dir = core_settings().claude_config_dir
    if config_dir:
        bases.append(Path(config_dir) / "projects")
    if working_dir:
        bases.append(Path(working_dir) / ".claude" / "projects")
    bases.append(Path.home() / ".claude" / "projects")
    unique: List[Path] = []
    for base in bases:
        if base not in unique:
            unique.append(base)
    return unique


def _find_claude_project_dir(working_dir: Optional[str] = None) -> Optional[Path]:
    """Find a .claude/projects directory that holds JSONL files.

    Claude stores sessions in {base}/projects/{project-key}/*.jsonl; the project-key is not known
    here, so any base with JSONL files qualifies. See ``_claude_project_bases`` for the order.
    """
    for base in _claude_project_bases(working_dir):
        try:
            if base.exists() and next(base.rglob("*.jsonl"), None) is not None:
                _log.info("_find_claude_project_dir: ✓ found .claude/projects with JSONL: %s", base)
                return base
        except Exception as e:
            _log.debug("_find_claude_project_dir: error searching %s: %s", base, e)

    _log.warning("_find_claude_project_dir: ✗ no .claude/projects dir with JSONL files found")
    return None


def _find_session_file(session_id: str, working_dir: Optional[str] = None) -> Optional[Path]:
    """The JSONL of the session launched with ``--session-id``, searched under EVERY base.

    Resolving one base first and then looking inside it is wrong when two bases exist and the
    worker wrote to the other one: the monitor would wait on a file that never appears there and
    the live view would show only heartbeats.

    Claude Code keeps a session at ``<base>/<project folder>/<session id>.jsonl``, so only each
    project folder is looked in. A recursive search walks every session ever recorded (35,000
    files on a busy machine), and the monitor asks every tenth of a second until the file
    appears: while another thread kept the interpreter busy that walk took 12 to 17 seconds.
    """
    for base in _claude_project_bases(working_dir):
        try:
            if not base.exists():
                continue
            direct = base / f"{session_id}.jsonl"
            if direct.is_file():
                return direct
            match = next(base.glob(f"*/{session_id}.jsonl"), None)
            if match is not None:
                return match
        except Exception as e:
            _log.debug("_find_session_file: error searching %s: %s", base, e)
    return None


def session_file_size(working_dir: Optional[str], session_id: Optional[str]) -> int:
    """Bytes already in the session's file, 0 when it has none yet. Never raises.

    A resumed worker appends to the file its earlier spawn wrote; the monitor starts after those
    bytes so a nudge or a retry does not replay everything the person has already seen."""
    if not session_id:
        return 0
    try:
        found = _find_session_file(session_id, working_dir)
        return found.stat().st_size if found is not None else 0
    except Exception:  # noqa: BLE001
        return 0


def _truncate_keep_end(text: str, limit: int) -> str:
    """Keep the END of a string that's too long to show in full, not the start.

    The end is the diagnostically important part: a command's distinguishing content (what
    actually runs, which file, which argument) usually comes AFTER any long, repetitive leading
    path (``cd`` into the same deep directory tree, a compound ``cd <long path> && <command>``).
    Truncating from the end instead made every such line render identically — this is what fooled
    the liveness reviewer into reading genuinely different commands as "stuck repeating the same
    action" when only their shared prefix was visible.
    """
    text = text.strip()
    if len(text) <= limit:
        return text
    return "..." + text[-limit:]


def _format_message_text(msg: dict) -> str:
    """Extract and format text content from a parsed JSONL message.

    Produces a clean, human-readable line for display in the task progress stream.
    Returns an empty string for messages with no meaningful content (bare type-only
    messages, tool_result blocks without text, etc.) so callers can skip them.
    """
    msg_type = msg.get("type", "unknown")

    # Extract message content blocks
    text_parts = []
    if "message" in msg and "content" in msg["message"]:
        content = msg["message"]["content"]

        # Handle string content (direct text from Claude Code session)
        if isinstance(content, str):
            text = content.strip()
            if text and len(text) > 0:
                if len(text) > 200:
                    text = text[:200].rstrip() + "..."
                text_parts.append(text)
        # Handle array of content blocks (Claude API format)
        elif isinstance(content, list):
            for block in content:
                block_type = block.get("type", "")
                if block_type == "text":
                    text = block.get("text", "").strip()
                    if text:
                        # Truncate very long text blocks — they're assistant narration, not output
                        if len(text) > 200:
                            text = text[:200].rstrip() + "..."
                        text_parts.append(text)
                elif block_type == "tool_use":
                    tool_name = block.get("name", "unknown")
                    # Format tool input as a brief label where possible
                    inp = block.get("input") or {}
                    if tool_name in ("Bash", "bash") and inp.get("command"):
                        cmd = str(inp["command"]).strip()
                        brief = _truncate_keep_end(cmd, 80)
                        text_parts.append(f"$ {brief}")
                    elif tool_name in ("Read", "read", "Write", "write") and inp.get("file_path"):
                        text_parts.append(f"{tool_name}: {_truncate_keep_end(str(inp['file_path']), 80)}")
                    elif tool_name in ("Edit", "edit") and inp.get("file_path"):
                        text_parts.append(f"Edit: {_truncate_keep_end(str(inp['file_path']), 80)}")
                    elif tool_name in ("WebSearch", "WebFetch") and (inp.get("query") or inp.get("url")):
                        target = str(inp.get("query") or inp.get("url", ""))
                        text_parts.append(f"{tool_name}: {_truncate_keep_end(target, 80)}")
                    else:
                        text_parts.append(f"Using {tool_name}")
                elif block_type == "tool_result":
                    # Tool results are noisy; skip them (the tool_use line is enough context)
                    pass
                elif block_type == "thinking":
                    think = block.get("thinking", "").strip()
                    if think:
                        brief = (think[:120] + "...") if len(think) > 120 else think
                        text_parts.append(f"[thinking] {brief}")

    # Only emit assistant turns with real content; skip bare user/tool_result lines
    if msg_type == "assistant" and text_parts:
        return " ".join(text_parts)
    if msg_type not in ("assistant", "user") and text_parts:
        return " ".join(text_parts)
    return ""


def _monitor_claude_session(
    working_dir: str,
    callback: Callable[[ProgressEvent], None],
    stop_event: threading.Event,
    cutoff_time: Optional[float] = None,
    poll_interval: float = 0.1,
    max_wait_seconds: float = 30.0,
    forced_run_id: Optional[str] = None,
    session_id: Optional[str] = None,
    heartbeat_interval: float = DEFAULT_MONITOR_HEARTBEAT_SECONDS,
    start_offset: int = 0,
) -> None:
    """Monitor a Claude Code session file and stream updates via callback.

    Runs in a background thread FOR THE ENTIRE LIFETIME OF THE SUBPROCESS — it never gives up
    early. The caller sets ``stop_event`` only when the subprocess itself has exited, timed out,
    or errored, so this loop's lifetime tracks the subprocess exactly. While otherwise quiet
    (before the session file exists, or once it exists but nothing new has been written), it
    emits a periodic liveness beat through ``callback`` every ``heartbeat_interval`` seconds, so a
    hung subprocess is never silently indistinguishable from a working one (silence longer than
    about 10s while a deep run is working is a bug by definition).

    ``session_id``, when given, is the id the worker was launched with (``--session-id <uuid>``).
    The monitor then watches EXACTLY that session's file, deterministically, instead of "any new
    jsonl" — this is what keeps it from cross-attaching to an unrelated concurrent session (the
    parent conversation, or another deep run sharing the same project dir). When absent (an older
    caller that didn't pass one), it falls back to the previous any-new-file heuristic.

    ``forced_run_id``, when given, is used for every emitted event's ``run_id`` instead of
    deriving one per session file. A goal retry spawns a brand-new subprocess (and therefore a
    brand-new Claude Code session file each attempt); without a forced id, each attempt would be
    tagged with a different run_id (the new session file's own stem, when the "TASK N [xxxxxxxx]"
    marker isn't echoed back in the assistant's reply text), which a consumer's dashboard would
    then render as a separate, duplicate deep-run entry for what is really one ongoing subgoal.

    ``start_offset`` is how many bytes of the session file were already there when the worker was
    launched (``session_file_size``): a resumed session keeps its earlier output in the same file,
    and only what the resumed worker adds is new.
    """
    _log.info("monitor thread started for working_dir: %s (session_id=%s)", working_dir, session_id)
    if cutoff_time is None:
        cutoff_time = time.time()
    monitor_start = time.time()
    last_beat = monitor_start
    beat_run_id = forced_run_id or (session_id[:8] if session_id else None)
    model_said: List[str] = []       # the model this session reported, once

    def maybe_emit_heartbeat(note: str) -> None:
        nonlocal last_beat
        now = time.time()
        if now - last_beat < heartbeat_interval:
            return
        last_beat = now
        elapsed = now - monitor_start
        callback(ProgressEvent(
            type=EVENT_EXEC,
            text=f"Still working, {note}",
            data={"run_id": beat_run_id, "phase": "heartbeat", "elapsed_seconds": round(elapsed, 1)},
        ))

    try:
        project_dir = _find_claude_project_dir(working_dir)
        if not project_dir:
            _log.warning("✗ project dir not found: checked %s and QAR_DEEP_WORKING_DIR/QAR_CORPUS_ROOT",
                        working_dir)
            _log.info("waiting for .claude/projects to be created...")
            # Wait for it to be created, then find it. Unlike before, this does NOT give up after
            # max_wait_seconds — it keeps beating and looking until the subprocess itself ends
            # (stop_event set). max_wait_seconds only controls how it logs, not when it quits.
            wait_start = time.time()
            logged_slow_wait = False
            while not stop_event.is_set() and not project_dir:
                maybe_emit_heartbeat("waiting for session output")
                if not logged_slow_wait and time.time() - wait_start > max_wait_seconds:
                    _log.warning("still waiting for .claude/projects after %.0fs, continuing",
                                 max_wait_seconds)
                    logged_slow_wait = True
                time.sleep(poll_interval)
                project_dir = _find_claude_project_dir(working_dir)
                if project_dir:
                    _log.info("✓ .claude/projects created: %s", project_dir)
                    break

        if not project_dir:
            # The subprocess ended (stop_event set) before ANY project dir ever appeared. Nothing
            # to watch, but this is not itself an error worth escalating: the caller's own
            # timeout/exit-code handling covers the outcome.
            _log.warning("monitor ending: no .claude/projects dir ever appeared for %s", working_dir)
            return

        _log.info("✓ found claude project dir: %s", project_dir)
        event_count = 0
        watched_files: Dict[str, int] = {}  # filename -> last known file position (bytes)
        file_session_ids: Dict[str, str] = {}  # filename -> task UUID

        # Fix for cross-attach: when we know the exact session id the worker was launched with,
        # watch ONLY that file (resolved once found, then just stat'd each poll). Otherwise fall
        # back to the prior "any new jsonl" heuristic for backward compatibility.
        target_file: Optional[Path] = None

        # Snapshot JSONL files that already exist so the any-new-file fallback never mistakes the
        # parent session (or any other pre-existing session) for a new deep-runner subprocess
        # session. mtime alone is insufficient: the parent session's JSONL gets a new mtime
        # whenever Claude does anything in this conversation, which can push it past cutoff_time.
        # Only that fallback needs the snapshot, and taking it walks every session file there is.
        pre_existing_files: set = set() if session_id else {str(f) for f in project_dir.rglob("*.jsonl")}
        _log.debug("pre-existing session files excluded from monitoring: %d", len(pre_existing_files))

        while not stop_event.is_set():
            # Periodically check for the session file(s) to read.
            try:
                if session_id:
                    if target_file is None:
                        found = _find_session_file(session_id, working_dir)
                        if found is not None:
                            target_file = found
                            _log.info("✓ bound to deep run's own session file: %s", target_file)
                    candidate_files = [target_file] if target_file is not None else []
                else:
                    # Any new jsonl not present before the subprocess started.
                    candidate_files = [
                        f for f in project_dir.rglob("*.jsonl") if str(f) not in pre_existing_files
                    ]

                for jsonl_file in candidate_files:
                    try:
                        file_key = str(jsonl_file)

                        # Track file position, not content hash (simpler, no duplicates)
                        if file_key not in watched_files:
                            _log.debug("detected session file: %s", jsonl_file.name)
                            # From the beginning, except the part a resumed session already held.
                            watched_files[file_key] = start_offset if session_id else 0

                        # Read new lines from this session file (only new ones we haven't seen)
                        current_size = jsonl_file.stat().st_size
                        last_pos = watched_files[file_key]

                        if current_size > last_pos:
                            try:
                                with open(jsonl_file, 'r') as f:
                                    f.seek(last_pos)
                                    for line in f:
                                        try:
                                            msg = json.loads(line.strip())
                                            msg_type = msg.get("type", "")

                                            # Only emit assistant messages (the actual work output)
                                            if msg_type not in ("assistant", "message"):
                                                continue

                                            # The exact model the worker is running on, said while it runs (the
                                            # result names it only after the run): its first message carries it.
                                            live_model = str((msg.get("message") or {}).get("model") or "")
                                            if live_model and not live_model.startswith("<") and not model_said:
                                                model_said.append(live_model)
                                                callback(ProgressEvent(
                                                    type=EVENT_STATUS, text=f"Running on {live_model}",
                                                    data={"deep_run_model_live": {"model": live_model,
                                                                                  "run_id": beat_run_id}}))

                                            # Format and emit the message as a progress event
                                            msg_text = _format_message_text(msg)
                                            if msg_text and msg_text.strip():
                                                event_count += 1
                                                last_beat = time.time()  # real activity counts as a beat

                                                # A caller-supplied id (the subgoal's stable task_uuid) always
                                                # wins, so every retry's fresh subprocess/session still reports
                                                # under the SAME run_id instead of spawning a duplicate entry.
                                                if forced_run_id:
                                                    run_id = forced_run_id
                                                # Extract task UUID from Claude's output (e.g., "TASK 1 [a1b2c3d4]")
                                                elif file_key not in file_session_ids:
                                                    import re
                                                    match = re.search(r'\[([a-f0-9]{8})\]', msg_text)
                                                    if match:
                                                        run_id = match.group(1)
                                                    else:
                                                        run_id = jsonl_file.stem[:8]
                                                    file_session_ids[file_key] = run_id
                                                else:
                                                    run_id = file_session_ids[file_key]

                                                _log.debug("emitting exec event #%d from %s: %s",
                                                         event_count, run_id, msg_text[:60])
                                                callback(ProgressEvent(
                                                    type=EVENT_EXEC,
                                                    text=msg_text,
                                                    data={"run_id": run_id, "message_type": msg_type, "event_number": event_count}
                                                ))
                                        except json.JSONDecodeError:
                                            pass

                                # Update file position to end
                                watched_files[file_key] = current_size
                            except FileNotFoundError:
                                pass
                    except Exception as e:
                        _log.debug("error processing session file %s: %s", jsonl_file.name, e)

            except Exception as e:
                _log.debug("error scanning project dir: %s", e)

            # Heartbeat: keep the caller informed even when nothing new has appeared yet. Real
            # activity above already refreshed ``last_beat``, so this only fires on quiet stretches
            # and never floods the stream while the worker is genuinely producing output.
            note = "session started, no new output yet" if watched_files else "waiting for session output"
            maybe_emit_heartbeat(note)

            time.sleep(poll_interval)

    except Exception as e:
        _log.warning("claude session monitor failed: %s", e)


# --- End-of-run diagnostics read from the run's OWN session record ----------------------------
#
# A deep run can do a large amount of real work (reads, writes, edits, commands) and then die
# BEFORE it ever prints its final ``--output-format json`` envelope: killed, OOM'd, crashed, or
# stuck in a loop until something reaped it. stdout is then empty, so the failure the human reads
# used to carry NOTHING about what the run did — even though a complete record of it exists on
# disk, in the very session JSONL the live monitor thread above was already tailing.
#
# These helpers read the TAIL of that same file at the end of a failed run. Two deliberate reuse
# decisions:
#   * the file is located through the SAME ``_find_claude_project_dir`` + ``--session-id`` match
#     the monitor uses, so there is exactly ONE definition of "where this run's session file is";
#   * each record is rendered by the SAME ``_format_message_text`` the monitor uses for live
#     progress, so the end-of-run summary reads like the progress stream instead of being a second,
#     divergent parser for the same format.
#
# This runs SYNCHRONOUSLY on the failure path, so it is bounded (reads at most the last
# ``SESSION_TAIL_MAX_BYTES``) and silent (never raises; any problem just yields "", and the caller
# falls back to the bare message). It reports only what the record SHOWS — it never asserts a cause.

SESSION_TAIL_MAX_BYTES = 256_000   # only the last chunk of a possibly huge session file is read


SESSION_TAIL_MAX_ACTIONS = 12      # how many recent actions/messages to surface


SESSION_TAIL_MAX_CHARS = 1500      # cap on the rendered block, matching the stdout tail cap


# At or below this length the worker's final output carries no real diagnostic value (a bare "ok",
# a stray newline), so the session record is worth appending even though stdout wasn't empty.
SESSION_TAIL_THIN_OUTPUT_CHARS = 40


def resolve_session_file(working_dir: Optional[str], session_id: Optional[str]) -> Optional[Path]:
    """The session JSONL file for ``session_id``, or None if it can't be located.

    Uses ``_find_session_file`` — the monitor thread's own resolution — on purpose: two
    independent implementations of "find the session file" would drift apart. Never raises.
    """
    if not session_id:
        return None
    try:
        return _find_session_file(session_id, working_dir)
    except Exception as e:  # noqa: BLE001 — diagnostics must never become a new failure mode
        _log.debug("could not resolve session file for %s: %s", session_id, e)
        return None


def session_replay_tokens(working_dir: Optional[str], session_id: Optional[str]) -> Optional[int]:
    """About how many tokens resuming ``session_id`` would replay ahead of the next prompt.

    Sums the message content of every user and assistant record after the session's last compact
    boundary (Claude Code replays only what follows one), measured with the prompt budget's own
    estimate. None when the file cannot be found or read, which callers treat as "unknown, resume
    as before" rather than as a reason to refuse. Never raises.
    """
    from . import prompt_budget
    path = resolve_session_file(working_dir, session_id)
    if path is None:
        return None
    chars = 0
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                kind = rec.get("type")
                if kind == "system" and rec.get("subtype") == "compact_boundary":
                    chars = 0
                    continue
                if kind not in ("user", "assistant"):
                    continue
                content = (rec.get("message") or {}).get("content")
                chars += len(content) if isinstance(content, str) else len(json.dumps(content or ""))
    except OSError:
        return None
    return int(chars / prompt_budget.CHARS_PER_TOKEN)


def resolved_model_from_session(working_dir: Optional[str], session_id: Optional[str],
                                *, max_bytes: int = 400_000) -> Optional[str]:
    """The full model id Claude Code actually ran this session with (e.g. ``claude-sonnet-4-5-...``).

    ``--model sonnet`` is an alias the CLI resolves itself, so the id only exists in the run's own
    session record: every assistant record carries ``message.model``. Reads the tail of the file and
    returns the most recent real id, skipping Claude Code's ``<synthetic>`` placeholder (its own
    error messages). None when the file is missing or holds no assistant turn. Never raises.
    """
    path = resolve_session_file(working_dir, session_id)
    if path is None:
        return None
    try:
        size = path.stat().st_size
        start = max(0, size - max_bytes)
        with open(path, "rb") as f:
            if start:
                f.seek(start)
            blob = f.read(max_bytes)
    except Exception as e:  # noqa: BLE001 — unreadable record means no model note, never a failure
        _log.debug("could not read session file %s: %s", path, e)
        return None
    found: Optional[str] = None
    for line in blob.decode("utf-8", errors="replace").splitlines():
        try:
            msg = json.loads(line)
        except Exception:  # noqa: BLE001 — partial first line or a record mid-write
            continue
        inner = msg.get("message") if isinstance(msg, dict) else None
        model = inner.get("model") if isinstance(inner, dict) else None
        if isinstance(model, str) and model.strip() and not model.startswith("<"):
            found = model.strip()
    return found


def subagent_work_in_flight(path: Optional[Path], *, fresh_seconds: float = 300.0) -> Optional[str]:
    """Why a quiet parent session is NOT idle: a subagent is doing the work, or None.

    A foreground ``Agent``/``Task`` call blocks the parent, whose log then stays unchanged for the
    whole subagent run while the subagent writes its own file under ``<session>/subagents/``. Two
    signals, either one is enough: a subagent file touched within ``fresh_seconds``, or an
    ``Agent``/``Task`` tool_use in the parent's tail with no matching tool_result yet. Never raises.
    """
    if path is None:
        return None
    try:
        sub_dir = path.with_suffix("") / "subagents"
        if sub_dir.is_dir():
            now = time.time()
            for f in sub_dir.glob("*.jsonl"):
                if now - f.stat().st_mtime < fresh_seconds:
                    return f"subagent {f.stem} wrote to its log {int(now - f.stat().st_mtime)}s ago"
        size = path.stat().st_size
        with open(path, "rb") as fh:
            fh.seek(max(0, size - 400_000))
            blob = fh.read()
        pending: dict = {}
        for line in blob.decode("utf-8", errors="replace").splitlines():
            try:
                rec = json.loads(line)
            except Exception:  # noqa: BLE001 — partial first line or a record mid-write
                continue
            content = (rec.get("message") or {}).get("content") if isinstance(rec, dict) else None
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_use" and block.get("name") in ("Agent", "Task"):
                    pending[block.get("id")] = block.get("name")
                elif block.get("type") == "tool_result":
                    pending.pop(block.get("tool_use_id"), None)
        if pending:
            return f"a {next(iter(pending.values()))} tool call is still running"
    except Exception as e:  # noqa: BLE001 — a failed check falls back to the normal review
        _log.debug("subagent in-flight check failed for %s: %s", path, e)
    return None


def read_session_activity_tail(
    working_dir: Optional[str],
    session_id: Optional[str],
    *,
    max_actions: int = SESSION_TAIL_MAX_ACTIONS,
    max_bytes: int = SESSION_TAIL_MAX_BYTES,
    max_chars: int = SESSION_TAIL_MAX_CHARS,
) -> str:
    """The last few human-readable actions from this run's session record, most recent last.

    Returns a bullet list ("- Read: <file>", "- $ <command>", "- <assistant text>") or "" when the
    file is missing, unreadable, or holds nothing renderable. Malformed/truncated lines are skipped
    individually — a session file cut off mid-write still yields every complete record before it.
    """
    path = resolve_session_file(working_dir, session_id)
    if path is None:
        return ""
    start = 0
    try:
        size = path.stat().st_size
        start = max(0, size - max_bytes)
        with open(path, "rb") as f:
            if start:
                f.seek(start)
            blob = f.read(max_bytes)
    except Exception as e:  # noqa: BLE001 — deleted, permissions, unreadable: no diagnostics, no crash
        _log.debug("could not read session file %s: %s", path, e)
        return ""

    lines = blob.decode("utf-8", errors="replace").splitlines()
    if start and lines:
        lines = lines[1:]  # the first line is a partial record, we seeked into the middle of it
    actions: List[str] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except Exception:  # noqa: BLE001 — a truncated/mid-write record; the rest is still usable
            continue
        if not isinstance(msg, dict):
            continue
        # Same filter as the live monitor: assistant turns are the actual work (tool_result records
        # arrive as ``user`` entries and are noise).
        if msg.get("type") not in ("assistant", "message"):
            continue
        try:
            rendered = _format_message_text(msg)
        except Exception:  # noqa: BLE001 — an unexpected record shape shouldn't lose the others
            continue
        if rendered and rendered.strip():
            actions.append(rendered.strip())

    if not actions:
        return ""
    block = "\n".join(f"- {a}" for a in actions[-max_actions:])
    if len(block) > max_chars:
        block = block[-max_chars:]  # keep the MOST RECENT end, that's the diagnostic part
    return block


def describe_session_activity(
    working_dir: Optional[str],
    session_id: Optional[str],
    *,
    worker_output: str = "",
) -> str:
    """A ready-to-append "what this run actually did" block, or "" when nothing is readable.

    Deliberately phrased as an observation of the record ("its session record shows it last did"),
    never as a diagnosis: the record proves what happened, not why the run failed.
    """
    block = read_session_activity_tail(working_dir, session_id)
    if not block:
        return ""
    lead = (
        "The worker produced no final output, but its own session record shows it last did"
        if not (worker_output or "").strip()
        else "Its own session record shows it last did"
    )
    return f"{lead} (most recent last):\n{block}"


# --- What the run's OWN record says it changed ------------------------------------------------------------
#
# The metric flow (``core/goal_metrics.py``) must know whether a worker changed something, and a Claude Code worker
# reports no write receipt. Its session record does hold the answer, structurally: every tool use is a ``tool_use`` block
# that NAMES the tool, and its result comes back as a ``tool_result`` block that says whether it was an error. A file
# changed when a file-changing tool was used and its result was not an error. Only those two fields are read: never a
# message's words, never a shell command's text (hard rule #3). A change made through a shell command (``sed -i``, a
# redirect, ``git apply``) is not a file-changing tool use and cannot be told from any other command, so it is not counted:
# 0 means none were seen, not that none happened.

# The tools Claude Code changes files with, as its transcripts name them (compared without regard to case).
FILE_CHANGE_TOOLS = frozenset({"edit", "write", "multiedit", "notebookedit"})


def session_record_paths(path: Path) -> List[Path]:
    """The run's session record and the records of the subagents it started (``<session>/subagents/*.jsonl``)."""
    paths = [path]
    try:
        paths.extend(sorted((path.with_suffix("") / "subagents").glob("*.jsonl")))
    except OSError:
        pass
    return paths


def landed_file_changes(working_dir: Optional[str], session_id: Optional[str]) -> Set[str]:
    """The ids of the tool uses in the session's record that changed a file and finished without an error.

    A tool use with no result yet (the worker was stopped in the middle of it) is not counted, and neither is one whose
    result was an error (a refused permission, an edit that matched nothing). Empty when the record cannot be found or
    read. Never raises."""
    path = resolve_session_file(working_dir, session_id)
    if path is None:
        return set()
    uses: Set[str] = set()
    failed: Set[str] = set()
    answered: Set[str] = set()
    for record_path in session_record_paths(path):
        try:
            with open(record_path, "r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if '"tool_use"' not in line and '"tool_result"' not in line:
                        continue   # most lines are text; the two block types are the only ones read
                    try:
                        content = (json.loads(line).get("message") or {}).get("content")
                    except (ValueError, AttributeError):
                        continue
                    for block in content if isinstance(content, list) else []:
                        if not isinstance(block, dict):
                            continue
                        if block.get("type") == "tool_use" and str(block.get("name") or "").lower() in FILE_CHANGE_TOOLS:
                            uses.add(str(block.get("id") or ""))
                        elif block.get("type") == "tool_result":
                            answered.add(str(block.get("tool_use_id") or ""))
                            if block.get("is_error") is True:
                                failed.add(str(block.get("tool_use_id") or ""))
        except OSError as e:
            _log.debug("could not read session record %s: %s", record_path, e)
    uses.discard("")
    return (uses & answered) - failed


def worker_output_is_thin(output: Optional[str]) -> bool:
    """Whether the worker's final output is too small to diagnose anything from.

    Empty is the main case (the worker died before printing its envelope); a couple of words is the
    same situation in practice, so both earn the session-record fallback.
    """
    return len((output or "").strip()) <= SESSION_TAIL_THIN_OUTPUT_CHARS
