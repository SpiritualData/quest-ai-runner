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
  * ``list_models`` — returns ``[]``: the CLI has no models.list, so the ModelRegistry falls back
                to its last-known tier map. Tier ids are mapped to the CLI's family aliases
                (``haiku``/``sonnet``/``opus``) so a tier always resolves to "latest of family".

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
from typing import Any, Dict, List, Optional

from ..core.adapters import ModelProviderBase

# Tools the spawned planner/answer process is barred from. plan/answer are PURE completions —
# the orchestrator does its own retrieval (the RetrievalAdapter) and its own deep execution (the
# DeepRunner). Letting the headless model wander off to read files / browse here would be slow and
# off-contract, so we disable the agentic tools and keep it a single-shot text generation.
_PURE_COMPLETION_DISALLOWED = (
    "Bash", "Read", "Edit", "Write", "Glob", "Grep",
    "WebSearch", "WebFetch", "Task", "NotebookEdit",
)

# The CLI accepts a family alias ("haiku"/"sonnet"/"opus") that always points at the latest model
# of that family — which is exactly what the ModelRegistry intends a tier to mean. We map any
# concrete id the registry hands us onto its family alias so resolution is robust whether the id is
# pinned ("claude-haiku-4-5"), date-suffixed, or already an alias.
_FAMILY_ALIASES = ("opus", "sonnet", "haiku")


def cli_model(model: Optional[str]) -> Optional[str]:
    """Map a resolved model id to a CLI-acceptable model arg.

    If the id names a known family it becomes that family's alias (latest of family); otherwise it
    is passed through unchanged (a fully-qualified id the CLI also accepts). ``None`` → ``None``
    (let the CLI use its default model).
    """
    if not model:
        return None
    low = model.strip().lower()
    for fam in _FAMILY_ALIASES:
        if fam in low:
            return fam
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

    def __init__(
        self,
        *,
        claude_path: str = "claude",
        timeout_seconds: float = 180.0,
        extra_path_dirs: Optional[List[str]] = None,
        disallowed_tools: Optional[List[str]] = None,
    ):
        self.claude_path = claude_path
        self.timeout_seconds = timeout_seconds
        self.extra_path_dirs = extra_path_dirs
        # Default to the pure-completion lockdown; a consumer may override (e.g. to [] to allow
        # the headless model its full tool set, though that is rarely what plan/answer want).
        self.disallowed_tools = (
            list(disallowed_tools) if disallowed_tools is not None
            else list(_PURE_COMPLETION_DISALLOWED)
        )

    # --- subprocess plumbing -------------------------------------------------

    def _build_env(self) -> dict:
        env = os.environ.copy()
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
        binary = self.claude_path
        if os.path.sep not in binary:
            resolved = shutil.which(binary)
            if resolved:
                binary = resolved
        return binary

    def _invoke(self, prompt: str, *, model: Optional[str], system: Optional[str] = None) -> str:
        """Run one headless ``claude -p`` completion and return the model's text.

        Uses ``--output-format json`` and returns the envelope's ``result`` field. Raises
        RuntimeError on a non-zero exit or unparseable envelope so callers can decide how to
        degrade (plan() swallows it to a safe default; answer() propagates).

        The prompt is piped via stdin (not passed as a CLI argument) so large prompts do not
        hit the OS ARG_MAX limit.
        """
        # Pass "-p" with no inline prompt — the CLI reads from stdin when no prompt arg follows.
        cmd: List[str] = [self._resolve_binary(), "-p", "--output-format", "json"]
        cli_m = cli_model(model)
        if cli_m:
            cmd += ["--model", cli_m]
        if system:
            cmd += ["--append-system-prompt", system]
        if self.disallowed_tools:
            cmd += ["--disallowed-tools", ",".join(self.disallowed_tools)]

        proc = subprocess.run(
            cmd,
            input=prompt.encode("utf-8"),
            env=self._build_env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=self.timeout_seconds,
        )
        out = (proc.stdout or b"").decode("utf-8", errors="replace")
        if proc.returncode != 0:
            err = (proc.stderr or b"").decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"claude CLI exited {proc.returncode}: {err or 'no stderr'}")
        try:
            envelope = json.loads(out)
        except (ValueError, TypeError) as e:
            raise RuntimeError(f"claude CLI returned non-JSON output: {e}")
        if isinstance(envelope, dict):
            if envelope.get("is_error"):
                raise RuntimeError(f"claude CLI reported an error: {envelope.get('result') or out[:200]}")
            return envelope.get("result") or ""
        return ""

    # --- ModelProvider surface ----------------------------------------------

    def plan(self, prompt: str, *, model: str, tool_schema: Dict[str, Any]) -> Dict[str, Any]:
        # The CLI can't force tool_choice, so append a hard instruction to emit ONLY the tool's
        # JSON object, then parse it leniently. normalize_decision tolerates a partial/empty dict.
        schema = tool_schema.get("input_schema", {}) if tool_schema else {}
        instruction = (
            "\n\n--- OUTPUT FORMAT (STRICT) ---\n"
            "Respond with ONLY a single JSON object recording your decision — no prose, no "
            "explanation, no markdown code fence around it. The object must conform to this JSON "
            "schema (include at least the required fields):\n"
            f"{json.dumps(schema, ensure_ascii=False)}\n"
            "Output the JSON object and nothing else."
        )
        try:
            text = self._invoke(prompt + instruction, model=model)
        except Exception:  # noqa: BLE001 — a planner hiccup must never break the loop
            return {}
        return extract_json_object(text)

    def answer(self, messages: List[Dict[str, Any]], *, model: str, system: Optional[str] = None) -> str:
        prompt = _flatten_messages(messages)
        return self._invoke(prompt, model=model, system=system)

    def list_models(self) -> List[str]:
        # The CLI has no models.list; let ModelRegistry use its last-known fallback tier map.
        # cli_model() maps those tier ids to family aliases when we actually invoke.
        return []
