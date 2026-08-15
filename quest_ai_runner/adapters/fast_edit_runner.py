"""FastEditRunner — a ``DeepRunner`` that lands a bounded file edit in ONE model call.

WHY IT EXISTS
-------------
The reference deep worker (``core.goal_runner.SubprocessGoalRunner``) spawns Claude Code with
``claude -p``: a full autonomous agent, up to ``--max-turns 30``, with an hour-long timeout. That
is the right tool for open-ended work, and absurd for "fix the typo in the second paragraph of
that doc". Worse, it is WASTEFUL BY CONSTRUCTION: by the time the brain decides to execute, it has
already assembled the relevant context (cards, reads, conversation slice). Spawning a fresh agent
throws all of that away and pays a second time for it to be rediscovered.

This runner takes the other path. It hands the model the context QAR already gathered plus the
current content of the candidate files, asks for the edit in one call, applies the result
in-process through a ``FileWriter``, and returns. Roughly one round trip instead of six, and no
agent startup.

WHERE IT SITS — a rung, not a replacement
-----------------------------------------
It is the FIRST rung of the orchestrator's deep-runner ladder, never the only one. The brain's
existing goal loop runs an attempt, verifies it against the written done-standard, and retries;
with a ladder wired, attempt 1 is this runner and attempt 2+ is the full deep runner. So a fast
edit that turns out to be insufficient, or that cannot even identify a target file, fails exactly
the way a weak model's attempt already fails, and the loop escalates on its own. There is no new
"is this a small edit?" decision anywhere: the cheap path is simply tried first and verified.

WHAT KEEPS IT HONEST
--------------------
  * It can only touch files that were ALREADY in this turn's context. Candidate paths are read out
    of the goal/brief/context preamble, then each is resolved through the ``FileWriter``'s
    boundary and must exist. An edit block naming anything outside that candidate set is rejected
    at apply time, so the model cannot widen its own blast radius.
  * If it cannot identify a candidate file it does nothing at all and returns ``met=False``, which
    escalates. Doing nothing is always available to it, and is the failure mode by design.
  * Every write goes through the ``FileWriter`` (containment, secret-file refusal, backup). This
    module never opens a file for writing itself.
  * A SEARCH block that does not match leaves the file untouched and produces Aider's specific
    diagnostic (which lines it did find, whether the replacement is already present). That
    diagnostic feeds ONE in-process retry; past that, escalating is cheaper than arguing.

WIRE FORMAT — chosen by file size, not by cleverness
----------------------------------------------------
  * Short files (``whole_file_max_lines``, 400 by default): the model returns the file's COMPLETE
    new content and applying it is a write. That step cannot fail to apply, which for a small doc
    fix makes it strictly more reliable than any matching scheme; the cost is output tokens
    proportional to file size, which is why it is bounded by line count.
  * Longer files: SEARCH/REPLACE blocks, applied with the content-anchored matcher vendored from
    Aider (``quest_ai_runner.vendor.aider_editblock``). Content-anchored, NOT line-number-anchored:
    models are unreliable about line numbers, and Aider measured a 9x increase in editing errors
    when flexible matching was removed.
"""
from __future__ import annotations

import difflib
import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..core.adapters import (
    EVENT_EXEC,
    FUTURE_CONTEXT_VIA_FIELD,
    DeepResult,
    DeepRunnerBase,
    FileWriter,
    ModelProvider,
    ProgressEvent,
)
from ..core.model_registry import ModelRegistry
from ..vendor.aider_editblock import (
    DEFAULT_FENCE,
    apply_edit,
    find_original_update_blocks,
    find_similar_lines,
    find_whole_file_blocks,
)

_log = logging.getLogger("quest-ai-runner.fast_edit_runner")

# EVENT_EXEC phases this runner emits. "editing"/"applying" are deliberately non-terminal strings
# (core/guard.py classifies only done/completed/failed/... as terminal), so an intermediate tick
# can never be read as the subgoal succeeding. Only the final tick uses a terminal phase.
PHASE_TARGETS = "targets"
PHASE_EDITING = "editing"
PHASE_APPLYING = "applying"
PHASE_DONE = "done"
PHASE_ERROR = "error"

# A token that looks like a file path: at least one dot with a short extension, optional
# directories. Used ONLY to nominate CANDIDATES; every candidate is then checked against the real
# filesystem through the writer's boundary, so a false positive costs nothing and a miss simply
# escalates to the full deep runner.
_PATH_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_./\\-]*\.[A-Za-z0-9]{1,10}")

WHOLE_FILE_SYSTEM = """You are a precise file editor. You make the smallest change that fully satisfies the request, and you change nothing else.

You will be given a goal, the context already gathered about it, and the FULL current content of one or more files.

Reply with the COMPLETE new content of every file you need to change, and nothing else. Use exactly this format, once per file:

path/to/file.ext
```
<the entire new file content>
```

Rules:
- The path goes alone on the line directly above the opening fence, and must be one of the paths you were given.
- Emit the file's ENTIRE content. Never skip, omit or elide any part of it, and never write "... existing content ..." or a comment standing in for content. Anything you leave out will be deleted from the file.
- Preserve the file's existing style, indentation, and trailing newline.
- Only include a file you are actually changing. If no file needs to change, reply with the single word NO_EDIT and nothing else.
- Check FIRST whether the content already satisfies the goal (a previous attempt may already have applied it, or the file may already have been correct). If so, that file needs no change: do not re-apply, duplicate, or repeat anything already present, even if the goal still asks for it.
- Do not explain, apologize, or add commentary before or after the blocks."""

SEARCH_REPLACE_SYSTEM = """You are a precise file editor. You make the smallest change that fully satisfies the request, and you change nothing else.

You will be given a goal, the context already gathered about it, and the current content of one or more files.

Reply only with SEARCH/REPLACE blocks. Use exactly this format, once per change:

path/to/file.ext
```
<<<<<<< SEARCH
the exact existing lines to find
=======
the lines to replace them with
>>>>>>> REPLACE
```

Rules:
- The path goes alone on the line directly above the opening fence, and must be one of the paths you were given.
- The SEARCH section must match the existing file EXACTLY, character for character, including all whitespace, indentation, comments and docstrings.
- Keep each SEARCH section as short as it can be while still matching only one place in the file. Include just enough surrounding lines to make it unique.
- Each block changes the FIRST match only. Use several blocks for several changes.
- To append to a file, leave the SEARCH section empty.
- Check FIRST whether the content already satisfies the goal (a previous attempt may already have applied it). If the change you would make is already present, that file needs no change: do not duplicate or repeat it.
- If no file needs to change, reply with the single word NO_EDIT and nothing else.
- Do not explain, apologize, or add commentary before or after the blocks."""

# Returned by the model when it judges that nothing needs changing. It is a STRUCTURED answer the
# model was asked to give, not a keyword sniffed out of free prose: the prompt defines it as the
# whole reply, and it is compared against the whole reply. (See CLAUDE.md hard rule #3 — the rule
# forbids inferring a decision from wording the model chose, not honouring a format it was told to
# emit.) Either way the consequence is the same and safe: no edit, met=False, escalate.
NO_EDIT_SENTINEL = "NO_EDIT"


def _normalize_trailing_newline(new_content: str, original_content: str) -> str:
    """Re-apply the ORIGINAL file's trailing-newline convention to a whole-file rewrite.

    The system prompt tells the model to preserve the file's existing trailing newline, but
    small/cheap models routinely drift by a blank line or two regardless. Trusting the model's
    own trailing whitespace means a request that is already fully satisfied can still come back
    looking like a "different" rewrite purely from that drift, which defeats the no-op check in
    ``apply_response`` and, on a goal-loop retry against an already-correct file, pads the file
    with another stray blank line every time.
    """
    stripped_new = new_content.rstrip("\n")
    trailing = original_content[len(original_content.rstrip("\n")):]
    return stripped_new + trailing


def _diff_snippet(path: str, before: str, after: str, *, max_lines: int = 40) -> str:
    """A short unified diff: evidence a goal-verification judge can actually confirm.

    Without this, a successful edit's report was just "Edited notes.md" -- no evidence of what
    changed. A judge asked to verify a goal against that alone has nothing to confirm and, with
    a weaker model, unreliably calls it not-met, sending a genuinely-finished edit back through
    the goal loop for another attempt. For a non-idempotent request ("append a line") that
    retry does not no-op, it repeats the append -- so the report being evidence-poor was not just
    a wasted call, it was a data-corruption risk. Bounded in size: this rides in an LLM prompt.
    """
    diff_lines = list(difflib.unified_diff(
        before.splitlines(keepends=True), after.splitlines(keepends=True),
        fromfile=path, tofile=path, n=1,
    ))
    if not diff_lines:
        return ""
    if len(diff_lines) > max_lines:
        diff_lines = diff_lines[:max_lines] + [f"... ({len(diff_lines) - max_lines} more lines)\n"]
    return "".join(diff_lines)


@dataclass
class FastEditConfig:
    """Knobs for the fast-edit path. Every default is deliberately conservative."""
    # At or below this many lines, a file is rewritten whole (an apply step that cannot fail).
    # Above it, SEARCH/REPLACE. 400 lines is a few thousand output tokens, cents at Sonnet-class
    # pricing, and covers the documentation and config edits this path exists for.
    whole_file_max_lines: int = 400
    # How many candidate files may be put in front of the model at once. A fast edit is meant to
    # be bounded; a request that genuinely spans many files belongs in the full deep runner.
    max_target_files: int = 4
    # Skip any candidate larger than this (bytes). A file this big is not a fast edit.
    max_file_bytes: int = 400_000
    # Total file content included in one prompt (bytes).
    max_total_bytes: int = 600_000
    # Model tier used when the orchestrator does not pin a model for the attempt. NOT the cheapest
    # tier: what makes this path cheap is the ARCHITECTURE (one call instead of a 30-turn agent),
    # not a weak model, and this call decides a real file write.
    tier: str = "quality"
    # In-process retries after a failed apply, fed the specific match diagnostic. Aider allows 3;
    # one is right here, because rung 2 of the ladder (the full deep runner) is a better use of
    # the next attempt than a third argument with the same model.
    max_retries: int = 1


class FastEditRunner(DeepRunnerBase):
    """One-call file editing behind the ``DeepRunner`` interface.

    Args:
        provider: the model provider. MUST be the ``MultiProvider``-wrapped one (``build_orchestrator``
            wraps ``cfg.model_provider`` in place before this runner is built), so the model id
            routes to whichever backend owns it.
        writer: the ``FileWriter`` granting write access. Required — there is no default and none
            is constructed implicitly. No writer, no runner.
        registry: used to resolve ``config.tier`` when the caller does not pin a model. Built from
            the provider when not supplied.
    """

    # Its output is a mechanical edit report rather than composed prose, and it knows the reusable
    # facts (which files changed) exactly and for free, so it fills ``future_context`` itself
    # rather than being asked to append a section to its payload.
    future_context_channel = FUTURE_CONTEXT_VIA_FIELD

    def __init__(self, *, provider: ModelProvider, writer: FileWriter,
                 registry: Optional[ModelRegistry] = None,
                 config: Optional[FastEditConfig] = None):
        self.provider = provider
        self.writer = writer
        self.registry = registry or ModelRegistry(provider)
        self.config = config or FastEditConfig()

    # --- target selection ----------------------------------------------------

    def candidate_files(self, *texts: Optional[str]) -> List[str]:
        """Paths named in this turn's material that really exist inside the writable root.

        Nomination is a text scan; the DECISION is the filesystem. A token only becomes a
        candidate if the writer resolves it inside the root (symlinks and ``..`` normalized,
        secret-ish names refused) and it is a readable file. Order follows the order the texts are
        passed, so the goal's own mention outranks something merely present in the context.
        """
        seen: List[str] = []
        for text in texts:
            if not text:
                continue
            for token in _PATH_RE.findall(text):
                token = token.strip().strip("`\"'").rstrip(".,;:)")
                if not token or token in seen:
                    continue
                resolved = getattr(self.writer, "resolve", lambda _p: None)(token)
                if resolved is None or not resolved.is_file():
                    continue
                try:
                    if resolved.stat().st_size > self.config.max_file_bytes:
                        continue
                except OSError:
                    continue
                seen.append(token)
                if len(seen) >= self.config.max_target_files:
                    return seen
        return seen

    def load_targets(self, paths: List[str]) -> List[Tuple[str, str]]:
        """Read each candidate, dropping anything unreadable, under the total-bytes budget."""
        loaded: List[Tuple[str, str]] = []
        total = 0
        for path in paths:
            content = self.writer.read_file(path)
            if content is None:
                continue
            size = len(content.encode("utf-8"))
            if total + size > self.config.max_total_bytes:
                break
            total += size
            loaded.append((path, content))
        return loaded

    def choose_format(self, targets: List[Tuple[str, str]]) -> str:
        """"whole" or "search_replace", decided by the LARGEST target.

        One call emits one format, so the decision is per call rather than per file, and the
        largest file is what sets the risk: rewriting it whole is what would be expensive.
        """
        biggest = max((c.count("\n") + 1 for _, c in targets), default=0)
        return "whole" if biggest <= self.config.whole_file_max_lines else "search_replace"

    # --- prompt --------------------------------------------------------------

    def build_prompt(self, *, goal: str, brief: str, context_preamble: Optional[str],
                     targets: List[Tuple[str, str]], fmt: str,
                     retry_feedback: Optional[str] = None) -> str:
        parts: List[str] = [f"GOAL (the done-standard for this edit):\n{goal}"]
        if brief and brief.strip() != goal.strip():
            parts.append(f"FULL REQUEST AND BRIEF:\n{brief}")
        if context_preamble:
            parts.append("CONTEXT ALREADY GATHERED (do not go looking for more; this is what is "
                         f"known):\n{context_preamble}")
        header = ("FILES YOU MAY EDIT (full current content follows; you may not edit any other "
                  "file):" if fmt == "whole" else
                  "FILES YOU MAY EDIT (current content follows; you may not edit any other file):")
        rendered = [header]
        for path, content in targets:
            rendered.append(f"\n{path}\n{DEFAULT_FENCE[0]}\n{content}\n{DEFAULT_FENCE[1]}")
        parts.append("\n".join(rendered))
        if retry_feedback:
            parts.append("YOUR PREVIOUS ATTEMPT DID NOT APPLY. Fix it and reply again in the same "
                         f"format:\n{retry_feedback}")
        return "\n\n".join(parts)

    # --- apply ---------------------------------------------------------------

    def apply_response(self, response: str, targets: List[Tuple[str, str]], fmt: str
                       ) -> Tuple[List[str], List[str], Dict[str, str]]:
        """Apply the model's reply. Returns ``(edited_paths, failures, diffs)``.

        A failure is a human/model-readable diagnostic; the file it refers to is UNCHANGED. The
        two failure shapes that matter are a SEARCH block that matched nothing (Aider's
        ``SearchReplaceNoExactMatch`` report, including the nearest actual lines and whether the
        replacement is already present) and an edit naming a file outside the candidate set.
        ``diffs`` holds a short unified diff per actually-written path -- see ``_diff_snippet``.
        """
        allowed = {path: content for path, content in targets}
        edited: List[str] = []
        failures: List[str] = []
        diffs: Dict[str, str] = {}

        if fmt == "whole":
            blocks = list(find_whole_file_blocks(response, DEFAULT_FENCE, list(allowed)))
            if not blocks:
                failures.append("No complete-file block was found in the reply. Each file must be "
                                "its path on one line, then a fenced block holding the entire new "
                                "content.")
            for path, new_content in blocks:
                if path not in allowed:
                    failures.append(f"Refused an edit to {path!r}: it is not one of the files "
                                    f"provided for this edit.")
                    continue
                original = allowed[path]
                new_content = _normalize_trailing_newline(new_content, original)
                if new_content == original:
                    continue  # a no-op rewrite is not a failure and not an edit
                result = self.writer.write_file(path, new_content)
                if result.ok:
                    edited.append(path)
                    diffs[path] = _diff_snippet(path, original, new_content)
                else:
                    failures.append(f"Write to {path!r} was refused: {result.error}")
            return edited, failures, diffs

        try:
            blocks = list(find_original_update_blocks(response, DEFAULT_FENCE, list(allowed)))
        except ValueError as e:
            # Malformed markers. Aider raises with the partial content attached, which is exactly
            # the diagnostic a retry needs.
            return edited, [f"The SEARCH/REPLACE blocks were malformed: {e}"]

        # Accumulate per file so several blocks against one file compose, and so a file is written
        # once. A failure anywhere in a file's chain abandons that FILE (its earlier blocks are
        # discarded unwritten), because a partially applied chain is the one outcome worse than no
        # edit at all.
        pending: Dict[str, str] = {}
        broken: set = set()
        for block in blocks:
            if block[0] is None:
                # A shell-command block. This runner does not execute anything; work that needs a
                # command belongs in the full deep runner, and saying so escalates.
                failures.append("The reply asked to run a shell command. The fast edit path only "
                                "edits files.")
                continue
            path, before, after = block
            if path not in allowed:
                failures.append(f"Refused an edit to {path!r}: it is not one of the files provided "
                                f"for this edit.")
                continue
            if path in broken:
                continue
            current = pending.get(path, allowed[path])
            updated = apply_edit(current, before, after, DEFAULT_FENCE, path)
            if updated is None:
                broken.add(path)
                pending.pop(path, None)
                failures.append(self.no_match_report(path, before, after, allowed[path]))
                continue
            pending[path] = updated

        for path, new_content in pending.items():
            if new_content == allowed[path]:
                continue
            result = self.writer.write_file(path, new_content)
            if result.ok:
                edited.append(path)
                diffs[path] = _diff_snippet(path, allowed[path], new_content)
            else:
                failures.append(f"Write to {path!r} was refused: {result.error}")
        return edited, failures, diffs

    @staticmethod
    def no_match_report(path: str, before: str, after: str, content: str) -> str:
        """Aider's ``SearchReplaceNoExactMatch`` diagnostic, which is what makes a retry land."""
        report = [
            f"## SearchReplaceNoExactMatch: this SEARCH block did not match any lines in {path}",
            "<<<<<<< SEARCH", before.rstrip("\n"), "=======", after.rstrip("\n"),
            ">>>>>>> REPLACE",
        ]
        similar = find_similar_lines(before, content)
        if similar:
            report.append(f"\nDid you mean to match these actual lines from {path}?\n"
                          f"{DEFAULT_FENCE[0]}\n{similar}\n{DEFAULT_FENCE[1]}")
        if after and after in content:
            report.append(f"\nThe REPLACE lines are already present in {path}. This edit may "
                          f"already be done.")
        report.append("\nThe SEARCH section must match the file exactly, including all whitespace, "
                      "indentation and comments.")
        return "\n".join(report)

    # --- DeepRunner API ------------------------------------------------------

    def run_goal(self, *, goal: str, brief: str, model: Optional[str] = None,
                 max_turns: Optional[int] = None,
                 emit: Optional[Callable[[ProgressEvent], None]] = None,
                 context_preamble: Optional[str] = None,
                 run_id: Optional[str] = None) -> DeepResult:
        started = time.time()

        def tick(phase: str, text: str, **data: Any) -> None:
            if emit is None:
                return
            try:
                emit(ProgressEvent(type=EVENT_EXEC, text=text,
                                   data={"run_id": run_id, "phase": phase, **data}))
            except Exception:  # noqa: BLE001 — streaming must never break the run
                pass

        try:
            paths = self.candidate_files(goal, brief, context_preamble)
            targets = self.load_targets(paths) if paths else []
            if not targets:
                # NOT an error state: it is this runner declining the goal, which is how the ladder
                # is meant to work. Non-empty output so the goal loop treats it as an attempt to
                # escalate past rather than a terminal failure.
                tick(PHASE_ERROR, "No editable file identified for a fast edit.")
                return DeepResult(
                    met=False,
                    output="No fast edit was attempted: this goal did not name a file inside the "
                           "writable root, so there was nothing to edit directly.",
                    error="fast edit: no candidate file found in the goal or its context")

            fmt = self.choose_format(targets)
            tick(PHASE_TARGETS, "Editing " + ", ".join(p for p, _ in targets),
                 files=[p for p, _ in targets], edit_format=fmt)

            run_model = model or self.registry.resolve_tier(self.config.tier)
            system = WHOLE_FILE_SYSTEM if fmt == "whole" else SEARCH_REPLACE_SYSTEM
            feedback: Optional[str] = None
            tokens = 0
            failures: List[str] = []
            edited: List[str] = []
            diffs: Dict[str, str] = {}

            for attempt in range(self.config.max_retries + 1):
                prompt = self.build_prompt(goal=goal, brief=brief,
                                           context_preamble=context_preamble,
                                           targets=targets, fmt=fmt, retry_feedback=feedback)
                tick(PHASE_EDITING,
                     "Working out the edit…" if attempt == 0 else "Re-trying the edit…",
                     attempt=attempt + 1)
                try:
                    response = self.provider.answer([{"role": "user", "content": prompt}],
                                                    model=run_model, system=system) or ""
                except Exception as e:  # noqa: BLE001 — a DeepRunner never raises
                    _log.warning("fast edit: model call failed", exc_info=True)
                    return DeepResult(met=False,
                                      output="The fast edit could not run because the model call "
                                             "failed.",
                                      error=f"fast edit: model call failed: {type(e).__name__}: {e}")
                # An ESTIMATE, not a report: ``ModelProvider.answer`` returns text with no usage,
                # so there is nothing authoritative to pass on. The goal loop only uses this to
                # spend down a budget, and an estimate that is roughly right is far better there
                # than a 0 that says this attempt was free. ~4 characters per token.
                tokens += max(0, (len(prompt) + len(response)) // 4)

                if response.strip() == NO_EDIT_SENTINEL:
                    tick(PHASE_ERROR, "No edit was needed.")
                    return DeepResult(
                        met=False,
                        output="No edit was made: the model judged that the files provided already "
                               "satisfy the goal.",
                        error="fast edit: model reported no change was needed", tokens=tokens)

                tick(PHASE_APPLYING, "Applying the edit…", attempt=attempt + 1)
                edited, failures, diffs = self.apply_response(response, targets, fmt)
                if edited and not failures:
                    break
                if attempt < self.config.max_retries:
                    feedback = "\n\n".join(failures) if failures else (
                        "No edit was applied. Reply again using the required format.")
                    # Re-read from disk so a retry works against the CURRENT content, including any
                    # block that already applied in the previous attempt.
                    targets = self.load_targets([p for p, _ in targets]) or targets

            elapsed = time.time() - started
            if edited and not failures:
                tick(PHASE_DONE, f"Edited {', '.join(edited)}", files=edited)
                # The goal-verification judge only ever sees this ``output`` string, never the
                # file itself -- a bare "Edited notes.md" gives it nothing to confirm a change
                # against, which is exactly what let a genuinely-successful edit get judged
                # not-met and retried (see CHANGELOG). The diff is the evidence.
                evidence = "\n\n".join(f"--- diff for {p} ---\n{diffs[p]}"
                                       for p in edited if diffs.get(p))
                summary = ("Edited " + ", ".join(edited) + f" in {elapsed:.1f}s "
                           "(one direct model call, no agent spawned)."
                           + (f"\n\n{evidence}" if evidence else ""))
                return DeepResult(
                    met=True, output=summary, tokens=tokens,
                    future_context="- Files changed by this run: " + ", ".join(edited))
            if edited and failures:
                # Partial: some files changed, some did not. Reported as NOT met so the goal loop
                # verifies and escalates, and the output names exactly what did land so the next
                # rung does not redo it.
                tick(PHASE_ERROR, "Some edits did not apply.")
                return DeepResult(
                    met=False,
                    output="Partly applied. Edited " + ", ".join(edited) +
                           ". These did not apply:\n" + "\n\n".join(failures),
                    error="fast edit: some edits did not apply", tokens=tokens,
                    future_context="- Files changed by this run: " + ", ".join(edited))
            tick(PHASE_ERROR, "The fast edit did not apply.")
            detail = ("\n\n" + "\n\n".join(failures)) if failures else (
                "\n\nThe reply contained no change to make: every block it returned matched the "
                "file's existing content.")
            return DeepResult(
                met=False,
                output="No fast edit was applied; the files are unchanged." + detail,
                error="fast edit: no edit applied", tokens=tokens)
        except Exception as e:  # noqa: BLE001 — a DeepRunner NEVER raises; it reports
            _log.error("fast edit runner failed: %s", e, exc_info=True)
            return DeepResult(met=False,
                              output="The fast edit path failed before it could change anything.",
                              error=f"fast edit: {type(e).__name__}: {e}")
