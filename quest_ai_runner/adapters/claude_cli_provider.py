"""ClaudeCliProvider — a KEYLESS ModelProvider that drives the local ``claude`` CLI headless.

The reference :class:`AnthropicProvider` calls the Anthropic SDK and therefore needs an
``ANTHROPIC_API_KEY`` (per-token billing). This provider instead shells out to the locally
installed ``claude`` binary in print mode (``claude -p``), so planning and answering run on the
operator's Claude Code **subscription login** — no API key, no per-token billing. It is the same
mechanism the deep-runner (:class:`~quest_ai_runner.core.goal_runner.SubprocessGoalRunner`) already
uses for the autonomous work; this brings the orchestrator's cheap planner/answer calls onto the
same keyless footing so the WHOLE runner can operate on a subscription alone.

It satisfies the same :class:`~quest_ai_runner.core.adapters.ModelProvider` interface:

  * ``plan``  — runs the planner prompt headless and asks the model to emit ONLY the ``decide``
                tool's JSON object (the CLI can't force ``tool_choice``, so we instruct + parse
                leniently). The brain's :func:`normalize_decision` is tolerant, so a malformed
                or empty parse degrades to a safe ``answer`` rather than crashing.
  * ``answer`` — flattens the message list into one headless prompt and returns the model's text.
  * ``list_models`` — the CLI has no models.list, so this advertises the Claude FAMILIES the CLI
                can run (``claude-haiku``/``claude-sonnet``/``claude-opus``) rather than an empty
                list. That keeps every tier on a model this provider can actually execute; an
                empty list made the ModelRegistry fall through to its Gemini-flavoured defaults,
                which no provider on a claude_cli-only deployment could run. Tier ids are mapped
                back to the CLI's bare family aliases on invoke, so a tier always resolves to
                "latest of family". (Fable is a runnable family too — see ``_FAMILY_ALIASES`` — but
                is deliberately not advertised here; see the note on ``CLI_RUNNABLE_MODELS`` below.)

Like the subprocess deep-runner, the spawned process has ``ANTHROPIC_API_KEY`` /
``ANTHROPIC_AUTH_TOKEN`` / ``CLAUDECODE`` stripped from its env so it can't reuse our own session
or fall back to API billing — it authenticates purely via the subscription login on the box.

Nothing here is consumer-specific: the binary path, timeouts, and tool gating are all config.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from functools import lru_cache
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..core import usage_limit
from ..core.adapters import ModelProviderBase, ThreadLocalCounter
from .retry_utils import retry_transient

# Tools the spawned planner/answer process is barred from. plan/answer are PURE completions —
# the orchestrator does its own retrieval (the RetrievalAdapter) and its own deep execution (the
# DeepRunner). Letting the headless model wander off to read files / browse here would be slow and
# off-contract, so we disable the agentic tools and keep it a single-shot text generation.
_PURE_COMPLETION_DISALLOWED = (
    "Bash", "Read", "Edit", "Write", "Glob", "Grep",
    "WebSearch", "WebFetch", "Task", "NotebookEdit",
)

# The system prompt used when a caller supplies none. ``--system-prompt`` REPLACES Claude Code's
# own agent system prompt rather than appending to it, so something must take its place.
_PURE_COMPLETION_SYSTEM = (
    "You are a precise assistant. Follow the user's instructions exactly "
    "and reply with only what they ask for."
)

# WHAT MAKES THIS A COMPLETION RATHER THAN AN AGENT, and why each flag is load-bearing.
#
# ``claude -p`` is not an API call with a thin CLI around it: by default it boots the whole Claude
# Code agent -- its system prompt, the schemas of every built-in tool, the settings sources, the
# CLAUDE.md files above the working directory, skills and plugins -- and only then answers. That
# default is right for the deep runner, which genuinely wants an agent. It is entirely wrong for
# this class, whose whole contract (see the module docstring) is "a single-shot text generation"
# used for planning, judging and indexing.
#
# MEASURED, on this CLI, asking only for the word "OK":
#   default (--append-system-prompt)          17,815 system tokens   $0.0368
#   + no settings / no MCP                      7,248 system tokens   $0.0159
#   + these flags                                   0 system tokens   $0.0017
# A 22x cost difference on every call, and the same multiple in work done inside each process.
# Multiplied by a parallel fan-out it is also the difference between a bounded indexing pass and
# one that pushes a loaded box into the OOM killer -- the harness, not the inference, is the cost.
#
# RE-MEASURED 2026-09-29, same "Say OK", and the "0 system tokens" line above had silently stopped
# being true on a newer CLI. Two of the holes it used to plug had re-opened, and together they are
# why AI-created tasks reported 200,000+ tokens for small work (see current_bugs/MEDIUM-008):
#   as the flags stood (--disallowed-tools, cwd = corpus root)   18,820 tokens   $0.0753
#   + --tools "" instead of --disallowed-tools                    9,896 tokens   $0.0396
#   + a neutral working directory                                    462 tokens   $0.0010
# Ten to fifteen such calls make an ordinary turn, so 18,820 each IS the 200k figure users saw:
# the count was honest, the harness behind it was not. Two causes, both now closed:
#   * ``--exclude-dynamic-system-prompt-sections`` no longer drops the built-in TOOL SCHEMAS.
#     ``--disallowed-tools`` never did: it blocks tool USE and still ships every schema, ~9,000
#     tokens of them. ``--tools ""`` removes the tools themselves, which is what a pure completion
#     wanted all along (it has no use for a tool it is forbidden to call).
#   * ``--setting-sources ""`` no longer keeps the CLAUDE.md chain out. The files above the
#     WORKING DIRECTORY are read regardless, and this runner deliberately runs from the corpus
#     root (the deep runner needs to see the whole tree), so every planner call was being handed
#     ~9,400 tokens of repo instructions it must not act on anyway. Hence ``cwd=`` below: the
#     completion subprocess runs in an empty scratch directory of its own.
#
#   --system-prompt                 REPLACE the agent prompt instead of appending to it, which is
#                                   what ``--append-system-prompt`` does (it keeps the whole agent
#                                   prompt and adds to it).
#   --exclude-dynamic-system-prompt-sections
#                                   drop the dynamically assembled sections (tool schemas, env
#                                   preamble). This is the flag that takes the overhead to zero;
#                                   it is only honoured alongside ``--system-prompt``.
#   --setting-sources ""            load no user/project/local settings -- so no CLAUDE.md, no
#                                   hooks, no skills, no plugins leak into an indexing call.
#   --strict-mcp-config             ignore every ambient MCP configuration. Without it, a call
#                                   spawns the operator's MCP servers too.
#
# ``--disallowed-tools`` is kept as well, but note what it is and is not: it blocks tool USE while
# still shipping the schemas. It is the belt to these flags' braces, not a substitute for them.
# Extended thinking is billed as OUTPUT, and for a call whose whole job is emitting a JSON array
# it is pure overhead. Measured on one topic-extraction call: 3,891 output tokens of which 1,746
# (45%) were thinking, to produce ~750 tokens of JSON. Across a full bootstrap output was 78% of
# the bill, so the reasoning budget alone was roughly a third of the total. ``QAR_CLI_EFFORT``
# passes the CLI's ``--effort`` through so a deployment can spend reasoning where it helps and not
# where it does not; unset leaves the CLI's own default alone.
_EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")


def _cli_effort() -> Optional[str]:
    """The ``--effort`` level to request, or None to leave the CLI's default alone."""
    raw = os.getenv("QAR_CLI_EFFORT", "").strip().lower()
    return raw if raw in _EFFORT_LEVELS else None


_ONE_SHOT_FLAGS: Dict[str, List[str]] = {
    "--exclude-dynamic-system-prompt-sections": ["--exclude-dynamic-system-prompt-sections"],
    "--setting-sources": ["--setting-sources", ""],
    "--strict-mcp-config": ["--strict-mcp-config"],
    "--tools": ["--tools", ""],        # no built-in tools AT ALL, so no schemas in the prompt
    "--effort": ["--effort"],          # presence-probed only; the value is appended in _invoke
}


def _neutral_cwd() -> str:
    """An empty directory to run a pure-completion subprocess in.

    The CLI reads the CLAUDE.md chain above its WORKING DIRECTORY no matter what the setting
    sources say, and this runner is deliberately started from the corpus root so the deep runner
    can see the whole tree. A planner/answer call inherited that cwd and was handed thousands of
    tokens of repository instructions on every single call: instructions it is not an agent for,
    cannot act on (it has no tools) and must not be steered by. Measured at ~9,400 tokens a call
    on this corpus, which is most of what made an ordinary turn report 200,000+ tokens used.

    An empty scratch directory has no CLAUDE.md above it that belongs to anyone's project, so the
    completion gets its prompt and nothing else. Falls back to the inherited cwd (``None``) if the
    directory cannot be made, because a slightly expensive call beats a call that cannot run.

    Deliberately NOT memoized (this used to be ``@lru_cache(maxsize=1)``, computing the path and
    creating the directory once per process). A long-running process (the poll/chat services run
    for days) outlives the directory: anything that clears stale scratch dirs under ``/tmp`` (a
    tmpfiles sweep, a reboot, a stray cleanup script) removes it, and the cached path then pointed
    at nothing -- every subsequent call failed with ``FileNotFoundError: ... 'qar-cli-completion'``
    from ``subprocess.run``'s ``cwd=``, with no way to recover short of restarting the process
    (found 2026-09-29: two Quest AI tasks failed back to back with exactly this error). Recomputed
    and `mkdir(exist_ok=True)`-ensured on every call instead, which is self-healing at the cost of
    one cheap, idempotent syscall per model call.
    """
    try:
        d = Path(tempfile.gettempdir()) / "qar-cli-completion"
        d.mkdir(parents=True, exist_ok=True)
        return str(d)
    except Exception:  # noqa: BLE001 -- never let scratch-dir trouble break a model call
        return None  # type: ignore[return-value]


@lru_cache(maxsize=8)
def _supported_flags(binary: str) -> frozenset:
    """Which of the one-shot flags THIS ``claude`` build accepts, probed once per binary.

    The flags above are recent. A deployment on an older CLI must keep working rather than fail
    every model call with a usage error, so we read ``--help`` once (cached for the life of the
    process) and pass only what it advertises. An unreadable ``--help`` yields the empty set: the
    call then runs exactly as it did before this hardening, which is degraded but never broken.
    """
    try:
        proc = subprocess.run([binary, "--help"], stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, timeout=60)
        help_text = (proc.stdout or b"").decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001 -- probing must never break the call it is preparing
        return frozenset()
    return frozenset(flag for flag in _ONE_SHOT_FLAGS if flag in help_text)


# The CLI accepts a family alias ("haiku"/"sonnet"/"opus"/"fable") that always points at the
# latest model of that family — which is exactly what the ModelRegistry intends a tier to mean. We
# map any concrete id the registry hands us onto its family alias so resolution is robust whether
# the id is pinned ("claude-haiku-4-5", "claude-fable-5-1"), date-suffixed, or already an alias.
_FAMILY_ALIASES = ("opus", "sonnet", "haiku", "fable")

# What ``list_models`` advertises (see its docstring for why this is not an empty list). Canonical
# ``claude-<family>`` ids so ModelRegistry.bucket_top buckets them into fast/balanced/quality and
# MultiProvider routes them by the "claude" prefix; cli_model() maps each back to the bare family
# alias when the CLI is actually invoked, so the runner always gets the latest of that family.
#
# Deliberately no "claude-fable" entry: ``bucket_top`` (core/model_registry.py) only recognizes
# haiku/opus/sonnet as Claude sub-families, so an unrecognized "claude-fable" would fall into its
# catch-all "claude-other" bucket, which no fast/balanced/quality/best candidate list consults —
# advertising it here would change nothing about tier resolution. A tier lands on Fable only via an
# explicit ``QAR_MODEL_*``/fallback override (a bare ``fable`` resolves through ``cli_model()``
# below like any other family alias), not through auto-bucketing.
CLI_RUNNABLE_MODELS = ["claude-opus", "claude-sonnet", "claude-haiku"]

# Standard per-user install locations the official Claude Code installer (and npm global
# install with a user-level prefix) places the binary in. Checked as a fallback, in this
# order, only when a bare "claude" isn't resolvable via PATH -- a process launched without
# a login shell's full PATH (a systemd --user unit, a cron job, a service manager) commonly
# has none of these directories on PATH even though `claude` works fine when the same user
# types it interactively. Home-relative, not any one machine's real absolute path, so this
# stays generic across consumers/deployments (repo hard rule #2).
_FALLBACK_CLAUDE_LOCATIONS = ("~/.local/bin/claude", "~/.claude/local/claude")


def cli_model(model: Optional[str]) -> Optional[str]:
    """Map a resolved model id to a CLI-acceptable model arg.

    If the id names a known family it becomes that family's alias (latest of family). A non-Claude
    id (e.g. ``gemini-3.1-flash-lite`` from a tier map built for a Gemini deployment) maps to
    ``None`` — the CLI only runs Claude models, and passing a foreign id as ``--model`` makes it
    exit 1 having done nothing (same trap the deep runner's ``_is_claude_model`` gate closes).
    Remaining Claude ids pass through unchanged (fully-qualified ids the CLI also accepts).
    ``None`` → ``None`` (let the CLI use its default model).
    """
    if not model:
        return None
    low = model.strip().lower()
    for fam in _FAMILY_ALIASES:
        if fam in low:
            # The CLI's own alias can lag a release; prefer the newest live id, alias as fallback.
            from .claude_live_models import newest_claude_id
            return newest_claude_id(fam) or fam
    if "claude" not in low and "anthropic" not in low:
        return None
    return model


def extract_json_object(text: str) -> Dict[str, Any]:
    """Best-effort parse the first JSON OBJECT out of a model's text reply.

    Handles the common shapes the CLI returns: a bare object, an object wrapped in a ```json fence,
    or an object embedded in surrounding prose. Returns ``{}`` if nothing parseable is found — the
    caller (the brain's normalize_decision) treats an empty dict as a safe default decision.
    """
    if not text:
        return {}
    s = text.strip()
    # Strip a leading/trailing markdown code fence if present.
    if s.startswith("```"):
        nl = s.find("\n")
        if nl != -1:
            s = s[nl + 1:]
        if s.rstrip().endswith("```"):
            s = s.rstrip()[: -3]
        s = s.strip()
    # Fast path: the whole thing is a JSON object.
    try:
        obj = json.loads(s)
        if isinstance(obj, dict):
            return obj
    except (ValueError, TypeError):
        pass
    # Fallback: scan for the first balanced {...} run and parse it.
    start = s.find("{")
    while start != -1:
        depth = 0
        in_str = False
        esc = False
        for i in range(start, len(s)):
            c = s[i]
            if in_str:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    in_str = False
                continue
            if c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    candidate = s[start : i + 1]
                    try:
                        obj = json.loads(candidate)
                        if isinstance(obj, dict):
                            return obj
                    except (ValueError, TypeError):
                        break  # this run didn't parse; try the next "{"
        start = s.find("{", start + 1)
    return {}


def _flatten_block(block: Any) -> str:
    """Render ONE content block to text for the keyless CLI, which is text-only.

    The CLI cannot send images natively, so the multimodal handler (core.attachments) is
    expected to have already converted any image to a text DESCRIPTION when the answering
    provider is this one. As a safety net this still degrades any block it is handed to text
    and NEVER raises on an unexpected shape:
      * a plain string → itself
      * a text block ``{"type": "text", "text": ...}`` → its text
      * an image block ``{"type": "image", ...}`` → a short placeholder note (we cannot inline
        the bytes); if the block carries a ``text``/``description`` it is used instead
      * anything else → its ``text``/``description`` if present, else a benign type note
    """
    if isinstance(block, str):
        return block
    if isinstance(block, dict):
        btype = block.get("type")
        if btype == "text" and block.get("text") is not None:
            return str(block.get("text"))
        # Some callers may attach a human-readable description alongside any block.
        for key in ("text", "description", "desc"):
            if block.get(key):
                return str(block.get(key))
        if btype == "image":
            return "[image attachment not viewable in text-only mode]"
        if btype:
            return f"[{btype} content]"
    # Last resort: stringify without raising.
    try:
        return str(block)
    except Exception:  # noqa: BLE001
        return ""


def _flatten_messages(messages: List[Dict[str, Any]]) -> str:
    """Render a chat message list into a single headless prompt (role-prefixed).

    ``content`` may be a plain string (the common path) OR a LIST of content blocks (text +
    image), the multimodal shape the orchestrator can produce. The CLI is text-only, so a block
    list is flattened block-by-block via ``_flatten_block`` — image blocks degrade to a short
    note rather than crashing the keyless backend.
    """
    parts: List[str] = []
    for m in messages or []:
        role = (m.get("role") or "user").upper()
        content = m.get("content")
        if isinstance(content, list):
            rendered = "\n".join(_flatten_block(b) for b in content)
        else:
            rendered = content or ""
        parts.append(f"{role}:\n{rendered}")
    return "\n\n".join(parts)


class ClaudeCliProvider(ModelProviderBase):
    """Keyless ModelProvider backed by the ``claude`` CLI (subscription login).

    Drop-in for :class:`AnthropicProvider` when the box has Claude Code logged in but no API key.
    """

    # The keyless CLI is text-only over its print-mode interface — it cannot transmit native image
    # content blocks. The multimodal handler reads this flag and routes images to describe-fallback
    # (transcribe to text) instead of trying to send blocks the CLI would flatten to a placeholder.
    supports_native_images = False

    # Thread-local (see ThreadLocalCounter / ModelProviderBase): this instance is shared across
    # concurrently running tasks (quest-ai-runner's Poller runs max_concurrent_tasks turns in
    # parallel on one Orchestrator/provider), so these must describe only the calling thread's
    # own usage. tokens_in/tokens_out/call_count come from ModelProviderBase; the extra
    # CLI-specific breakdown fields below need the same treatment.
    fresh_input_tokens = ThreadLocalCounter()
    cache_creation_tokens = ThreadLocalCounter()
    cache_read_tokens = ThreadLocalCounter()
    cost_usd = ThreadLocalCounter(default=0.0)

    def __init__(
        self,
        *,
        claude_path: str = "claude",
        timeout_seconds: float = 180.0,
        extra_path_dirs: Optional[List[str]] = None,
        disallowed_tools: Optional[List[str]] = None,
        plan_thinking_tokens: Optional[int] = None,
    ):
        super().__init__()
        # Cap on extended thinking for plan() calls only, passed to the CLI as MAX_THINKING_TOKENS.
        # Every plan() call on this provider gets it: routing decisions and the reach judge, and
        # also the overseer and cascade reviews when those run here (measured only on routing).
        # None leaves the CLI's default alone. Answers and deep runs are never affected. Measured 2026-10-06 with haiku as the planner on 100
        # labelled routing decisions: thinking on, 94% at 36.9s p50 and ~4,000 output tokens a
        # decision; thinking capped at 0, 93% at 3.3s p50 and ~160 (7 discordant, 4 against 3).
        self.plan_thinking_tokens = plan_thinking_tokens
        self.claude_path = claude_path
        self.timeout_seconds = timeout_seconds
        self.extra_path_dirs = extra_path_dirs
        # Default to the pure-completion lockdown; a consumer may override (e.g. to [] to allow
        # the headless model its full tool set, though that is rarely what plan/answer want).
        self.disallowed_tools = (
            list(disallowed_tools) if disallowed_tools is not None
            else list(_PURE_COMPLETION_DISALLOWED)
        )
        # Running totals, read by callers that report what a run cost (see _accumulate_usage).
        # All thread-local (see the class-level ThreadLocalCounter declarations above).
        self.tokens_in = 0
        self.tokens_out = 0
        self.fresh_input_tokens = 0
        self.cache_creation_tokens = 0
        self.cache_read_tokens = 0
        self.cost_usd = 0.0
        self.call_count = 0

    # --- subprocess plumbing -------------------------------------------------

    def _build_env(self, thinking_tokens: Optional[int] = None) -> dict:
        env = os.environ.copy()
        if thinking_tokens is not None:
            env["MAX_THINKING_TOKENS"] = str(max(0, int(thinking_tokens)))
        env["PYTHONUNBUFFERED"] = "1"
        # Force the SUBSCRIPTION login path: never let the headless run reuse our session or fall
        # back to API-key billing (mirrors SubprocessGoalRunner._build_env).
        env.pop("CLAUDECODE", None)
        env.pop("ANTHROPIC_API_KEY", None)
        env.pop("ANTHROPIC_AUTH_TOKEN", None)
        if self.extra_path_dirs:
            cur = env.get("PATH", "")
            for d in self.extra_path_dirs:
                if d and d not in cur:
                    cur = f"{d}:{cur}"
            env["PATH"] = cur
        return env

    def _resolve_binary(self) -> str:
        """The worker binary to spawn, re-resolved on EVERY call.

        Re-resolved rather than cached, and the configured path is CHECKED rather than trusted,
        because the binary is a moving target: an installer or auto-update replaces it in place,
        and for a few seconds the path that was valid when this provider was constructed does not
        exist. A long run walks straight into that window. Observed twice on one machine in a day
        -- the second time it cost 101 of 107 topic-extraction calls in a single bootstrap, each
        failing with a bare FileNotFoundError, and the run reported completion having produced
        almost nothing. Falling back costs one stat() per call and turns a fatal window into a
        blip.
        """
        binary = self.claude_path
        if os.path.sep in binary:
            path = Path(binary).expanduser()
            if path.is_file() and os.access(path, os.X_OK):
                return str(path)
            # The configured path is gone right now (mid-reinstall, or simply wrong). Fall through
            # to the same discovery a bare name gets, rather than spawning something that is not
            # there.
            binary = path.name
        resolved = shutil.which(binary)
        if resolved:
            return resolved
        # Not on PATH (e.g. a service-manager environment without the launching shell's
        # rc-file PATH additions) -- try the standard per-user install locations before
        # giving up, so subprocess.run doesn't fail with a bare FileNotFoundError when the
        # binary is genuinely installed, just not on THIS process's PATH.
        for candidate in _FALLBACK_CLAUDE_LOCATIONS:
            path = Path(candidate).expanduser()
            if path.is_file() and os.access(path, os.X_OK):
                return str(path)
        return self.claude_path

    @retry_transient(max_retries=3, base_delay=1.0)
    def _invoke(self, prompt: str, *, model: Optional[str], system: Optional[str] = None,
                thinking_tokens: Optional[int] = None) -> str:
        """Run one headless ``claude -p`` completion and return the model's text.

        Uses ``--output-format json`` and returns the envelope's ``result`` field. Raises
        RuntimeError on a non-zero exit or unparseable envelope so callers can decide how to
        degrade (plan() swallows it to a safe default; answer() propagates).

        The prompt is piped via stdin (not passed as a CLI argument) so large prompts do not
        hit the OS ARG_MAX limit.
        """
        # While the lane knows Claude Code is at its usage limit, do not spawn a CLI that can only
        # be refused: say so straight away, as the same typed error a live refusal raises. The
        # note clears itself at the reset (see core.usage_limit.active_limit).
        paused_on = usage_limit.active_limit()
        if paused_on is not None:
            raise usage_limit.UsageLimitError(paused_on)
        # Pass "-p" with no inline prompt — the CLI reads from stdin when no prompt arg follows.
        binary = self._resolve_binary()
        cmd: List[str] = [binary, "-p", "--output-format", "json"]
        cli_m = cli_model(model)
        if cli_m:
            cmd += ["--model", cli_m]
        # REPLACE the agent's system prompt rather than appending to it (see _ONE_SHOT_FLAGS).
        # Always passed, because --exclude-dynamic-system-prompt-sections is only honoured
        # alongside it, and because an absent system prompt would otherwise restore the agent's.
        supported = _supported_flags(binary)
        if "--exclude-dynamic-system-prompt-sections" in supported:
            cmd += ["--system-prompt", system or _PURE_COMPLETION_SYSTEM]
        elif system:
            # Older CLI: no way to drop the agent prompt, so keep the previous append behaviour.
            cmd += ["--append-system-prompt", system]
        for flag in _ONE_SHOT_FLAGS:
            if flag == "--effort":
                continue           # value-bearing, handled just below
            if flag in supported:
                cmd += _ONE_SHOT_FLAGS[flag]
        effort = _cli_effort()
        if effort and "--effort" in supported:
            cmd += ["--effort", effort]
        # Only needed as the FALLBACK. When ``--tools ""`` was accepted above there is no tool left
        # to disallow, and naming them again would only re-introduce the schemas we just removed.
        if self.disallowed_tools and "--tools" not in supported:
            cmd += ["--disallowed-tools", ",".join(self.disallowed_tools)]

        proc = subprocess.run(
            cmd,
            input=prompt.encode("utf-8"),
            env=self._build_env(thinking_tokens),
            cwd=_neutral_cwd(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=self.timeout_seconds,
        )
        out = (proc.stdout or b"").decode("utf-8", errors="replace")
        if proc.returncode != 0:
            err = (proc.stderr or b"").decode("utf-8", errors="replace").strip()
            # In --output-format json mode the CLI reports errors in the STDOUT envelope
            # ({"is_error": true, "result": "API Error: ..."}), often with an empty stderr —
            # surface that instead of a useless "no stderr".
            if not err and out:
                try:
                    envelope = json.loads(out)
                    if isinstance(envelope, dict) and envelope.get("result"):
                        err = str(envelope["result"])
                except (ValueError, TypeError):
                    err = out[:300].strip()
            # The subscription usage limit is not an error to report, it is a wait: raise it typed
            # so the lane can pause and the task can resume at the reset (core.usage_limit).
            limit = usage_limit.detect_usage_limit(err)
            if limit is not None:
                raise usage_limit.UsageLimitError(
                    usage_limit.record(limit),
                    f"claude CLI exited {proc.returncode}: {err}")
            raise RuntimeError(f"claude CLI exited {proc.returncode}: {err or 'no stderr'}")
        try:
            envelope = json.loads(out)
        except (ValueError, TypeError) as e:
            raise RuntimeError(f"claude CLI returned non-JSON output: {e}")
        if isinstance(envelope, dict):
            if envelope.get("is_error"):
                limit = usage_limit.detect_usage_limit(str(envelope.get("result") or ""))
                if limit is not None:
                    raise usage_limit.UsageLimitError(
                        usage_limit.record(limit),
                        f"claude CLI reported an error: {envelope.get('result')}")
                raise RuntimeError(f"claude CLI reported an error: {envelope.get('result') or out[:200]}")
            self._accumulate_usage(envelope)
            return envelope.get("result") or ""
        return ""

    def _accumulate_usage(self, envelope: Dict[str, Any]) -> None:
        """Add one call's reported usage to this provider's running totals. Never raises.

        WHY THIS EXISTS. Callers already read ``tokens_in``/``tokens_out`` off a provider to report
        what a run cost (``cli.py`` prints them after a bootstrap), and this provider never set
        them -- so every keyless deployment reported its bootstrap as costing zero tokens and
        nothing, which is not a cheap answer but an absent one. The CLI's own JSON envelope carries
        both the token counts and the price it computed, so the honest number is already in hand
        and only needed adding up.

        ``cost_usd`` is what the CLI itself reports for the call. On a subscription login that is a
        LIST-PRICE equivalent rather than money leaving an account, which is exactly the figure you
        want when asking "would this be affordable if it were metered" -- and it is the only number
        here that is measured rather than modelled.
        """
        try:
            usage = envelope.get("usage") or {}
            # Kept SEPARATE as well as summed, because the three bill at very different rates
            # (cache reads at roughly a tenth of fresh input) and a single total cannot be turned
            # back into a cost. A run reporting "1.6M input tokens" is somewhere between $0.50 and
            # $4.85 of input depending purely on this split, which is the difference between
            # affordable and not.
            self.fresh_input_tokens += int(usage.get("input_tokens") or 0)
            self.cache_creation_tokens += int(usage.get("cache_creation_input_tokens") or 0)
            self.cache_read_tokens += int(usage.get("cache_read_input_tokens") or 0)
            # ALL THREE count toward ``tokens_in``, because all three are input the model
            # genuinely processed on this call and all three are genuinely billed (fresh at list
            # price, cache creation at 1.25x, cache reads at about a tenth). Dropping any of them
            # does not make an expensive call cheap, it only makes the report wrong:
            #  * counting ``input_tokens`` alone under-reports a harness-heavy call by orders of
            #    magnitude (17,815 vs 9 on one measured call);
            #  * an earlier pass at MEDIUM-008 dropped cache READS on the theory that a read is a
            #    re-count of the creation that wrote it. It is not. A read is separately billed
            #    input on a separate call, and once the harness shrank (see the flag notes at the
            #    top of this module) a warm second call measured 2 fresh + 9,894 read, which that
            #    theory would have reported as "2 tokens used" for a real 9,896. The fix for an
            #    inflated number is to stop sending the tokens, which is what the flags now do,
            #    never to stop counting tokens that were sent.
            # The three stay separately readable above (``fresh_input_tokens`` /
            # ``cache_creation_tokens`` / ``cache_read_tokens``) because a single total cannot be
            # turned back into a cost.
            self.tokens_in += int(usage.get("input_tokens") or 0)
            self.tokens_in += int(usage.get("cache_creation_input_tokens") or 0)
            self.tokens_in += int(usage.get("cache_read_input_tokens") or 0)
            self.tokens_out += int(usage.get("output_tokens") or 0)
            self.cost_usd += float(envelope.get("total_cost_usd") or 0.0)
            self.call_count += 1
        except Exception:  # noqa: BLE001 -- accounting must never break the call it is measuring
            pass

    # --- ModelProvider surface ----------------------------------------------

    def plan(self, prompt: str, *, model: str, tool_schema: Dict[str, Any],
             layers: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
        # ``layers`` is accepted for interface parity but ignored: the CLI takes one flattened
        # prompt and has no prompt-cache surface to wire, so the caller's ``prompt`` is used as-is.
        # The CLI can't force tool_choice, so append a hard instruction to emit ONLY the tool's
        # JSON object, then parse it leniently. normalize_decision tolerates a partial/empty dict.
        schema = tool_schema.get("input_schema", {}) if tool_schema else {}
        instruction = (
            "\n\n--- OUTPUT FORMAT (STRICT) ---\n"
            "Respond with ONLY a single JSON object recording your decision — no prose, no "
            "explanation, no markdown code fence around it. The object must conform to this JSON "
            "schema (include at least the required fields):\n"
            # Compact separators: this schema rides the prompt on EVERY planner call on this
            # backend (the CLI has no native tool-use surface to pass it out of band), so its own
            # formatting whitespace is paid for every time.
            f"{json.dumps(schema, ensure_ascii=False, separators=(',', ':'))}\n"
            "Output the JSON object and nothing else."
        )
        try:
            # Only passed when set, so an _invoke override without the keyword keeps working.
            extra = ({} if self.plan_thinking_tokens is None
                     else {"thinking_tokens": self.plan_thinking_tokens})
            text = self._invoke(prompt + instruction, model=model, **extra)
        except usage_limit.UsageLimitError:
            # Not a hiccup: nothing further in this turn can run, so let the caller stop and wait
            # rather than planning on an empty default decision.
            raise
        except Exception:  # noqa: BLE001 — a planner hiccup must never break the loop
            return {}
        return extract_json_object(text)

    def answer(self, messages: List[Dict[str, Any]], *, model: str, system: Optional[str] = None,
               layers: Optional[List[Dict[str, Any]]] = None) -> str:
        # ``layers`` accepted for interface parity but ignored; ``messages`` are flattened as before.
        prompt = _flatten_messages(messages)
        return self._invoke(prompt, model=model, system=system)

    def list_models(self) -> List[str]:
        # The CLI has no models.list, but it can only ever run CLAUDE models, so this provider
        # must SAY so rather than return an empty list. Returning [] made ModelRegistry fall
        # through to DEFAULT_FALLBACK_TOP, whose fast/balanced/quality entries are Gemini ids: on
        # a claude_cli-only deployment every tier then resolved to a model no registered provider
        # could run ("Gemini model 'gemini-3.1-flash-lite' requested but Gemini provider not
        # registered"), and tasks died at the first planner call. Operators worked around it by
        # setting QAR_MODEL_FAST/BALANCED/QUALITY/BEST by hand, which is config every claude_cli
        # lane needs and any new lane silently forgets.
        #
        # These are FAMILY aliases in canonical id form, deliberately not pinned versions: the CLI
        # treats a family as "latest of that family", which is exactly what a tier means, and it
        # keeps this list from ageing. bucket_top() buckets them to fast/balanced/quality
        # (haiku/sonnet/opus) and cli_model() maps each back to the bare alias on invoke. An
        # explicit QAR_MODEL_* override still wins, since user fallbacks are applied last.
        return CLI_RUNNABLE_MODELS
