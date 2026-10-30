"""Tools the brain can CALL directly: a standard catalog plus custom tools an operator registers.

Why this exists: before it, the only way the brain could DO anything (send a quest email, file a
draft for review) was a deep run, a full ``claude -p`` subprocess that spends a minute or more
rediscovering how to do a one-command job. A tool is that one command made first-class: the
planner sees it, picks it with its arguments, the loop invokes it in-process, and the answer
reports the receipt. Seconds, not minutes, and no deep run at all.

Three ideas, all generic (nothing here knows about any org):

  * ``ToolSpec`` describes ONE tool the way a model needs to choose it: a description, WHEN TO
    USE it, WHEN NOT TO (the line that separates it from its neighbours), a JSON-schema of its
    arguments, and whether it mutates anything (a mutating call is an action with a receipt; a
    read-only call is just more gathered context).
  * ``ToolRegistry`` holds the catalog and does RELEVANCE SELECTION: the planner is shown only the
    tools that match the current request (BM25 over each tool's name, description, when-to-use
    and keywords), never the whole catalog every turn, and can search the rest with a
    ``{"tools": "<query>"}`` read. ``invoke`` validates, fills context/default arguments, runs with
    a timeout, and NEVER raises: a failure comes back as a ``ToolResult`` the loop records.
  * ``build_tool_registry(env)`` is the ONE standard way a deployment gets its catalog: the
    standard tools every QAR user has (``send_quest_email``, ``file_email_draft`` and one
    quest-data tool per quest field, goal, strategy and habit, whenever Quest credentials are
    configured), plus custom tools declared in TOML files named by
    ``QAR_TOOLS_FILE``. A custom
    tool is either a command (any executable, arguments passed as flags or JSON on stdin) or a
    Python ``module:function`` handler, and it appears in the same catalog, with the same
    when-to-use / when-not-to-use contract, as the standard ones.

Deep runs get the same catalog: ``render_deep_block`` writes the relevant tools, with the exact
shell command that calls each one (``python -m quest_ai_runner.tools call <name> --args '{..}'``),
into the deep brief, so a run that does happen calls the tool instead of reinventing it.

TOML shape (full reference: docs/tools.md)::

    [[tool]]
    name = "queue_email_for_review"
    description = "File an email draft in the review queue; a person approves before it sends."
    when_to_use = "Any email to someone outside the team."
    when_not_to_use = "Internal or team updates: use send_quest_email."
    mutates = true
    command = ["{python}", "/path/to/manage_email_draft.py", "create", "--json"]
    args_mode = "flags"                      # or "stdin_json"
    keywords = ["email", "draft", "review", "external"]

    [tool.parameters]
    type = "object"
    required = ["to", "subject", "body"]
    [tool.parameters.properties.to]
    type = "array"
    items = {type = "string"}
    flag = "--to"                            # array -> the flag repeats once per item
    description = "Recipient address(es)."

    [standard.send_quest_email]              # tune a standard tool without replacing it
    defaults = {quest_id = "quest_abc123"}
    when_to_use_extra = "Default quest for internal mail with no quest in context."
"""
from __future__ import annotations

import copy
import importlib
import json
import logging
import math
import os
import re
import shlex
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from .requester import apply_requester_env, requester_from_meta
from .runtime_settings import process_environment
from .tools_status import DONE_STATUSES, ToolStatus, as_status, status_of_error, write_status_of
from hubofhubs.act.tool_ledger import FileToolLedger, outcome_from_result

log = logging.getLogger(__name__)

# At or below this many tools the planner simply sees them all: selection only earns its keep when
# the catalog is big enough that listing it every turn would crowd the prompt.
SHOW_ALL_AT_OR_BELOW = 8
DEFAULT_SELECT_K = 5
PINNED_RUN_EXTRAS = 2
DEFAULT_TIMEOUT_SECONDS = 60.0
# A tool's result text is observation context, not a document: cap it so one chatty command
# cannot flood the planner.
MAX_RESULT_CHARS = 4000


@dataclass
class ToolContext:
    """What the loop knows about the current turn, offered to tools that declare they want it."""
    quest_id: Optional[str] = None
    user_id: Optional[str] = None
    task_id: Optional[str] = None
    meta: Dict[str, Any] = field(default_factory=dict)

    def value(self, key: str) -> Any:
        if key in ("quest_id", "user_id", "task_id"):
            return getattr(self, key)
        return (self.meta or {}).get(key)


@dataclass
class ToolResult:
    """What one tool call came to. ``status`` is the typed outcome (``core.tools_status.ToolStatus``): applied, held (waiting
    for a person's approval), noop (already so), refused, failed or unknown (sent, never answered). Leave it out for
    a plain success or failure; ``ok`` then picks applied or failed. Given, it decides ``ok``: only applied and noop are
    ok, so a write that is held or refused is never ok whatever the caller wrote."""
    ok: bool
    text: str = ""
    data: Any = None
    status: Any = None

    def __post_init__(self) -> None:
        self.status = as_status(self.status) or (ToolStatus.APPLIED if self.ok else ToolStatus.FAILED)
        self.ok = self.status in DONE_STATUSES


ToolHandler = Callable[[Dict[str, Any], ToolContext], Any]


def data_of(write: Any) -> Any:
    """The raw answer inside a typed client write result (a plain payload is returned as it is)."""
    return getattr(write, "data", write)


def write_failed(error: BaseException, nothing_text: str) -> ToolResult:
    """The result of a write that raised. A client write error names its own status (refused, failed, unknown) and its
    message already says what happened and why, so it is passed on as written; any other error is a plain failure
    (``nothing_text`` is then the lead-in, or empty to pass the error's own words on as they are)."""
    typed = getattr(error, "write_status", None) is not None
    return ToolResult(ok=False, status=status_of_error(error),
                      text=str(error) if typed or not nothing_text else f"{nothing_text}: {error}")


def result_of_write(write: Any, applied: str, *, held: Optional[str] = None, noop: Optional[str] = None,
                    empty: Optional[str] = None) -> ToolResult:
    """A tool result from what a client write returned: its typed status (or, for a plain payload, the flags the server
    sent) picks the status and the matching text. A write that returned nothing at all is a failure, never a success."""
    data = data_of(write)
    if write is None or (isinstance(write, (dict, list)) and not write and empty is not None):
        return ToolResult(ok=False, status=ToolStatus.FAILED, data=data, text=empty or "Nothing was written.")
    status = write_status_of(write)
    text = {ToolStatus.HELD: held, ToolStatus.NOOP: noop}.get(status) or applied
    return ToolResult(ok=status in DONE_STATUSES, status=status, data=data, text=text)




@dataclass
class UnavailableTool:
    """A standard tool this deployment does not offer, and the one line the planner reads when a request is about what that
    tool would do. It stops the AI promising (or inventing) what the deployment cannot do; it is shown only when one of its
    ``keywords`` shares a word with the request or one of its ``patterns`` (regular expressions, matched case-insensitively
    on the PERSON's message, never on model output) finds a match in it, so other requests carry no extra prompt text.
    Prefer ``patterns`` of a few specific words over single common ``keywords``: a common word costs the note on every
    unrelated request."""
    name: str
    note: str
    keywords: Sequence[str] = ()
    patterns: Sequence[str] = ()

    def about(self, query: str, asked: set) -> bool:
        if asked & {t for kw in self.keywords for t in tokenize(kw)}:
            return True
        return any(re.search(p, query or "", re.I) for p in self.patterns)


@dataclass
class ToolSpec:
    name: str
    description: str
    handler: ToolHandler
    when_to_use: str = ""
    when_not_to_use: str = ""
    parameters: Dict[str, Any] = field(default_factory=lambda: {"type": "object", "properties": {}})
    mutates: bool = True
    origin: str = "custom"          # "standard" (ships with QAR) or "custom" (operator-registered)
    keywords: Sequence[str] = ()
    defaults: Dict[str, Any] = field(default_factory=dict)
    # arg name -> ToolContext key: filled from the turn when the model leaves the arg out.
    context_args: Dict[str, str] = field(default_factory=dict)
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    # Whether Quest Workflows may offer this tool as a step action. None = the rule in
    # ``runner.action_catalog`` (tools that act, ``mutates``); True/False overrides it either way.
    workflow_step: Optional[bool] = None
    # Parameters a workflow step may never set: left out of the published form, and dropped from a
    # requested action's arguments at run time. For values only a live user (or the step's own
    # context) may supply, such as a recipient override or a claim that the user asked.
    workflow_hidden_params: Sequence[str] = ()
    # An operation that only makes sense when the run's composer asks for it by name (a standing
    # instruction authorises it, say). It is left out of relevance selection, ``tools list`` and the
    # "search the catalog" read, so a chat turn cannot drift into it; a run whose context lists it
    # in ``tool_pins`` is shown it, and it stays callable by name.
    on_request_only: bool = False
    # An operation EVERY deep run owes, whatever its brief says (a standing instruction asks for it, as
    # ``record_metric_result`` is asked for). ``render_deep_block`` lists it after the tools the brief's
    # words selected, so it never takes one of their places; a chat turn still never sees it when it is
    # also ``on_request_only``.
    standing_deep: bool = False
    # A tool that reads the specification records of the repository the run works in. It exists only in a process
    # that can find that repository (an ``esoi.toml`` above its working folder, or ``QAR_SPEC_ROOT``), and a worker's
    # shell is a new process that may start somewhere else, so ``render_deep_block`` hands such a tool's command the root.
    needs_spec_root: bool = False

    def required(self) -> List[str]:
        return list((self.parameters or {}).get("required") or [])

    def properties(self) -> Dict[str, Any]:
        return dict((self.parameters or {}).get("properties") or {})

    def public_parameters(self) -> Dict[str, Any]:
        """The schema as the model sees it: implementation keys (``flag``) stripped."""
        params = copy.deepcopy(self.parameters or {"type": "object", "properties": {}})
        for prop in (params.get("properties") or {}).values():
            if isinstance(prop, dict):
                prop.pop("flag", None)
        return params

    def public_listing(self) -> Dict[str, Any]:
        """A read-only, serializable description of this tool (no handler, no secrets): what a
        consumer such as the Quest backend needs to offer the tool as a catalog entry."""
        return {
            "name": self.name,
            "description": self.description,
            "when_to_use": self.when_to_use,
            "when_not_to_use": self.when_not_to_use,
            "mutates": bool(self.mutates),
            "origin": self.origin,
            "parameters": self.public_parameters(),
            "auto_filled": self.auto_filled(),
        }

    def auto_filled(self) -> List[str]:
        """Args the loop fills itself when omitted (a default or a turn-context value)."""
        return sorted(set(self.defaults) | set(self.context_args))


TOKEN_RE = re.compile(r"[a-z0-9]+")


# Words that say nothing about which tool fits. Without this a request such as "summarize this pdf
# of grants" matched every tool whose description contains "this" or "of", and a catalog entry with
# a long how-to was shown for no reason.
STOPWORDS = frozenset(
    "a an and are as at be but by do for from has have how i if in into is it its me my no not of "
    "on or our so that the their them then there these they this to up us was we what when which "
    "who will with you your".split())


def stem(word: str) -> str:
    """Strip a plural or past-tense ending so "asked", "asks" and "ask" meet (query and tool text alike)."""
    if len(word) > 5 and word.endswith("ing"):
        return word[:-3]
    if len(word) > 4 and word.endswith("ed"):
        return word[:-2]
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def tokenize(text: str) -> List[str]:
    return [stem(t) for t in TOKEN_RE.findall((text or "").lower().replace("_", " "))
            if t not in STOPWORDS]


def coerce_result(raw: Any) -> ToolResult:
    if isinstance(raw, ToolResult):
        return raw
    if isinstance(raw, str):
        return ToolResult(ok=True, text=raw)
    if isinstance(raw, dict):
        return ToolResult(ok=True, text=json.dumps(raw, default=str)[:MAX_RESULT_CHARS], data=raw)
    if raw is None:
        return ToolResult(ok=True, text="done")
    return ToolResult(ok=True, text=str(raw))


class ToolRegistry:
    """The catalog. Thread-safe for reads; registration happens at build time."""

    def __init__(self, specs: Optional[Sequence[ToolSpec]] = None,
                 ledger: Optional[FileToolLedger] = None,
                 metric_ledger: Optional[Any] = None):
        self._specs: Dict[str, ToolSpec] = {}
        self.unavailable: Dict[str, UnavailableTool] = {}
        # Where every call's outcome is written, keyed by the task it ran for (see
        # ``hubofhubs.act.tool_ledger``). None = nothing is recorded.
        self.ledger = ledger
        # Where a run's metric results are kept, keyed by the run (``core/metric_results.py``); the
        # ``record_metric_result`` tool writes to it and the goal loop reads it. None = the flow uses
        # the environment's default ledger.
        self.metric_ledger = metric_ledger
        for spec in specs or ():
            self.register(spec)

    # -- catalog ---------------------------------------------------------------------------------

    def register(self, spec: ToolSpec) -> None:
        if not spec.name or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_\-]*", spec.name):
            raise ValueError(f"invalid tool name {spec.name!r}")
        if spec.name in self._specs:
            log.info("tool %s re-registered (%s replaces %s)", spec.name, spec.origin,
                     self._specs[spec.name].origin)
        self._specs[spec.name] = spec

    def note_unavailable(self, entry: UnavailableTool) -> None:
        """Record that a standard tool is not offered here (ignored when a tool of that name is registered)."""
        if entry.name not in self._specs:
            self.unavailable[entry.name] = entry

    def unavailable_notes(self, query: str) -> List[str]:
        """The notes of the unavailable tools this request is about (a keyword shares a word with it, or a pattern matches it)."""
        asked = set(tokenize(query))
        return [u.note for u in self.unavailable.values() if u.name not in self._specs and u.about(query, asked)]

    def unregister(self, name: str) -> None:
        self._specs.pop(name, None)

    def get(self, name: str) -> Optional[ToolSpec]:
        return self._specs.get(name)

    def all(self) -> List[ToolSpec]:
        return list(self._specs.values())

    def names(self) -> List[str]:
        return list(self._specs)


    def __len__(self) -> int:
        return len(self._specs)

    def __bool__(self) -> bool:
        return bool(self._specs)

    # -- relevance -------------------------------------------------------------------------------

    def doc_tokens(self, spec: ToolSpec) -> List[str]:
        # Name and keywords count twice: they are the tool's own summary of what it is for.
        name_toks = tokenize(spec.name)
        kw_toks = [t for kw in spec.keywords for t in tokenize(kw)]
        # Only the description's first paragraph: a field tool appends its whole writing standard
        # ("HOW TO WRITE IT: ...") after a blank line, and those paragraphs are about how to write,
        # not what the tool is for.
        summary = spec.description.split("\n\n", 1)[0]
        return (name_toks * 2 + kw_toks * 2 + tokenize(summary)
                + tokenize(spec.when_to_use))

    def offered(self) -> List[ToolSpec]:
        """The tools relevance may show: everything except ``on_request_only`` operations."""
        return [s for s in self.all() if not s.on_request_only]

    def search(self, query: str, k: int = DEFAULT_SELECT_K) -> List[ToolSpec]:
        """BM25 over the catalog; only tools that share a term with the query are returned."""
        q = set(tokenize(query))
        specs = self.offered()
        if not q or not specs:
            return []
        docs = [self.doc_tokens(s) for s in specs]
        n = len(docs)
        avgdl = sum(len(d) for d in docs) / n or 1.0
        df: Dict[str, int] = {}
        for d in docs:
            for t in set(d):
                df[t] = df.get(t, 0) + 1
        k1, b = 1.5, 0.75
        # A word that most of the catalog shares ("quest", "update") says nothing about which tool
        # fits, yet on a quest-heavy brief it matched every field tool and showed five long how-tos
        # nobody needed. Only words that tell tools apart count.
        common_above = max(2, n // 3)
        scored = []
        for spec, d in zip(specs, docs):
            tf: Dict[str, int] = {}
            for t in d:
                tf[t] = tf.get(t, 0) + 1
            score = 0.0
            for t in q:
                if t not in tf or df[t] > common_above:
                    continue
                idf = math.log(1 + (n - df[t] + 0.5) / (df[t] + 0.5))
                score += idf * tf[t] * (k1 + 1) / (tf[t] + k1 * (1 - b + b * len(d) / avgdl))
            if score > 0:
                scored.append((score, spec))
        scored.sort(key=lambda x: -x[0])
        return [s for _, s in scored[:k]]

    def select(self, query: str, k: int = DEFAULT_SELECT_K,
               pins: Sequence[str] = ()) -> List[ToolSpec]:
        """The tools to SHOW the planner for this request.

        A small catalog is shown whole (the model is better at relevance than a lexical score, and
        eight entries cost little). A large one is narrowed to the top ``k`` matches; the rest stay
        reachable through a ``{"tools": "<query>"}`` read. ``pins`` are tools the run's composer
        named (``context_meta["tool_pins"]``): shown first whatever the query says, so an operation
        a kind of run always needs does not depend on a lexical match against a long brief.
        """
        pinned = [s for s in (self.get(n) for n in (pins or ())) if s is not None]
        offered = self.offered()
        # A composer that pins has said what the run needs; the query is then a long composed brief
        # whose words match tools by accident, so relevance only adds a couple of extras.
        k = min(k, PINNED_RUN_EXTRAS) if pinned else k
        found = offered if len(offered) <= SHOW_ALL_AT_OR_BELOW else self.search(query, k)
        seen = {s.name for s in pinned}
        return pinned + [s for s in found if s.name not in seen]

    # -- rendering -------------------------------------------------------------------------------

    def public_listing(self) -> List[Dict[str, Any]]:
        """Every tool as ``ToolSpec.public_listing()``, sorted by name."""
        return [spec.public_listing() for spec in sorted(self.all(), key=lambda t: t.name)]

    @staticmethod
    def render_one(spec: ToolSpec) -> str:
        lines = [f"- {spec.name} ({spec.origin}, {'ACTS' if spec.mutates else 'read-only'}): "
                 f"{spec.description.strip()}"]
        if spec.when_to_use:
            lines.append(f"    USE WHEN: {spec.when_to_use.strip()}")
        if spec.when_not_to_use:
            lines.append(f"    DO NOT USE WHEN: {spec.when_not_to_use.strip()}")
        props = spec.public_parameters().get("properties") or {}
        req = set(spec.required())
        auto = set(spec.auto_filled())
        arg_bits = []
        for pname, prop in props.items():
            ptype = (prop or {}).get("type", "string")
            if ptype == "array":
                ptype = f"array of {((prop or {}).get('items') or {}).get('type', 'string')}"
            tag = "required" if pname in req and pname not in auto else "optional"
            if pname in auto:
                tag += ", auto-filled if omitted"
            desc = ((prop or {}).get("description") or "").strip()
            arg_bits.append(f"      {pname} ({ptype}, {tag}){': ' + desc if desc else ''}")
        if arg_bits:
            lines.append("    ARGS:")
            lines.extend(arg_bits)
        return "\n".join(lines)

    def render_catalog(self, specs: Sequence[ToolSpec]) -> str:
        return "\n".join(self.render_one(s) for s in specs)

    def render_planner_block(self, query: str, k: int = DEFAULT_SELECT_K,
                             pins: Sequence[str] = ()) -> str:
        shown = self.select(query, k, pins)
        hidden = len(self.offered()) - len([s for s in shown if not s.on_request_only])
        head = ("TOOLS YOU CAN CALL DIRECTLY (action \"tool\"). Each runs in seconds, in-process: "
                "no deep run. The loop runs your calls, shows you the result, and you then answer "
                "the user with what happened.")
        rules = (
            "- Choose \"tool\" whenever a listed tool does what the user asked (e.g. sending an "
            "email a tool covers). Prefer it to \"deep\": a deep run for a job one tool call does "
            "is minutes wasted.\n"
            "- Read each tool's USE WHEN / DO NOT USE WHEN and pick the one whose USE WHEN fits; "
            "two tools can do similar things for different audiences.\n"
            "- Put calls in \"tool_calls\": [{\"name\": \"<tool>\", \"args\": {...}}]. Write "
            "the real content into the args (the full email body, not a placeholder). If a "
            "REQUIRED value is unknown and cannot be read, ask with \"clarify\" instead.\n"
            "- Calls run ONLY when action is \"tool\". With action \"answer\" nothing runs, so an "
            "answer saying it was sent would be false: to act this step, the action is \"tool\".\n"
            "- A call already in GATHERED is settled: never repeat it, give its receipt (HELD FOR APPROVAL = "
            "waiting, not done). If it failed, say so or try another way once.")
        body = self.render_catalog(shown) if shown else "(no tool matched this request)"
        body += "".join(f"\nNOT AVAILABLE HERE: {note}" for note in self.unavailable_notes(query))
        tail = (f"\n({hidden} more tool(s) not shown. Search them with a read spec "
                f"{{\"tools\": \"<what you need>\"}}.)" if hidden > 0 else "")
        return f"{head}\n{rules}\n{body}{tail}"

    def render_deep_block(self, query: str, k: int = DEFAULT_SELECT_K,
                          python: Optional[str] = None, pins: Sequence[str] = ()) -> str:
        """The tool section of a deep brief: relevant tools plus the exact command for each.

        The worker runs the command in its own shell, a new process whose folder is not this one's. A tool that needs
        the repository's specification records (``needs_spec_root``) is only found there when told where they are, so
        the command carries the root this process found them at, and such a tool is not named at all when there is
        none: a brief never names a tool its worker could not call."""
        shown = self.select(query, k, pins)
        standing = [s for s in self.all() if s.standing_deep and s not in shown]
        spec_root = None
        if any(s.needs_spec_root for s in shown + standing):
            from . import repo_specs
            spec_root = repo_specs.find_root()
            shown = [s for s in shown if spec_root is not None or not s.needs_spec_root]
            standing = [s for s in standing if spec_root is not None or not s.needs_spec_root]
        if not (shown or standing):
            return ""
        py = python or sys.executable
        cmd = f"{shlex.quote(py)} -m quest_ai_runner.tools"
        if spec_root is not None and any(s.needs_spec_root for s in shown + standing):
            cmd += f" --spec-root {shlex.quote(str(spec_root))}"
        parts = ["TOOLS YOU CAN CALL (use these instead of writing your own script for the same "
                 "job; each prints its result and exits 0 on success, 1 on failure):",
                 self.render_catalog(shown + standing),
                 f"Call one:   {cmd} call <name> --kw arg=value [--kw arg=value ...]   "
                 f"(or --args '<json object>' for nested values)",
                 f"Find more:  {cmd} list --query '<what you need>'"]
        example = (shown or standing)[0]
        example_kw = " ".join(f"--kw {shlex.quote(f'{p}=<{p}>')}" for p in example.required()
                              if p not in example.auto_filled())
        parts.append(f"Example:    {cmd} call {example.name} {example_kw}".rstrip())
        return "\n".join(parts)

    # -- invocation ------------------------------------------------------------------------------

    def resolve_args(self, spec: ToolSpec, args: Mapping[str, Any],
                     ctx: ToolContext) -> Dict[str, Any]:
        """Explicit args win, then turn context, then configured defaults."""
        resolved: Dict[str, Any] = {k: v for k, v in (args or {}).items()
                                    if v is not None and v != ""}
        for arg, ctx_key in (spec.context_args or {}).items():
            if arg not in resolved:
                val = ctx.value(ctx_key)
                if val not in (None, ""):
                    resolved[arg] = val
        for arg, val in (spec.defaults or {}).items():
            if arg not in resolved and val not in (None, ""):
                resolved[arg] = val
        return resolved

    def invoke(self, name: str, args: Optional[Mapping[str, Any]] = None,
               ctx: Optional[ToolContext] = None) -> ToolResult:
        spec = self.get(name)
        if spec is None:
            return ToolResult(ok=False, text=f"unknown tool {name!r}; available: "
                                             f"{', '.join(self.names()) or 'none'}")
        if args is not None and not isinstance(args, Mapping):
            return ToolResult(ok=False, text=f"{name}: args must be a JSON object")
        ctx = ctx or ToolContext()
        resolved = self.resolve_args(spec, args or {}, ctx)
        props = spec.properties()
        unknown = [a for a in resolved if props and a not in props]
        if unknown:
            return ToolResult(ok=False, text=f"{name}: unknown argument(s) {unknown}; "
                                             f"accepted: {sorted(props)}")
        missing = [r for r in spec.required() if r not in resolved]
        if missing:
            return ToolResult(ok=False, text=f"{name}: missing required argument(s) {missing}")
        for pname, prop in props.items():
            if pname in resolved and (prop or {}).get("type") == "array" \
                    and not isinstance(resolved[pname], list):
                resolved[pname] = [resolved[pname]]
            # A shell caller's ``--kw summary=2026`` arrives typed as JSON; a text argument is text.
            if pname in resolved and (prop or {}).get("type") == "string" \
                    and isinstance(resolved[pname], (bool, int, float)):
                resolved[pname] = json.dumps(resolved[pname])

        box: Dict[str, Any] = {}

        def run_handler():
            try:
                box["result"] = coerce_result(spec.handler(resolved, ctx))
            except Exception as e:  # a tool failure is a result, never a crash of the loop
                log.warning("tool %s raised: %s", name, e)
                # A client write error carries its own status (refused, failed, unknown) and a message that says so.
                typed = getattr(e, "write_status", None) is not None
                box["result"] = ToolResult(ok=False, text=str(e) if typed else f"{name} failed: {e}",
                                           status=status_of_error(e))

        t = threading.Thread(target=run_handler, name=f"tool-{name}", daemon=True)
        t.start()
        t.join(spec.timeout_seconds)
        if t.is_alive():
            # A write that was running when the time ran out may still land: unknown, not failed.
            result = ToolResult(ok=False, status=ToolStatus.UNKNOWN if spec.mutates else ToolStatus.FAILED,
                                text=f"{name} timed out after {spec.timeout_seconds:.0f}s "
                                     f"(it may still complete; check before retrying)")
        else:
            result = box.get("result") or ToolResult(ok=False, text=f"{name} returned nothing")
        if len(result.text) > MAX_RESULT_CHARS:
            result.text = result.text[:MAX_RESULT_CHARS] + " …[truncated]"
        if self.ledger is not None:
            # By the task, else by the run: a deep run with no task is read back under its run id.
            self.ledger.record(ctx.task_id or ctx.value("run_id"),
                               outcome_from_result(name, result.ok, result.text, spec.mutates))
        return result


# ---------------------------------------------------------------------------------------------
# Command-backed tools (the custom-tool path for anything that is not Python)
# ---------------------------------------------------------------------------------------------

_ENV_REF = re.compile(r"\$(?:(\w+)|\{([^}]*)\})")


def _expand_env_vars(token: str) -> str:
    """``os.path.expandvars`` semantics over the injected environment: ``$VAR`` and ``${VAR}``
    expand when defined and are left as written when not."""
    env = process_environment()

    def one(m: "re.Match[str]") -> str:
        name = m.group(1) or m.group(2)
        return env[name] if name in env else m.group(0)

    return _ENV_REF.sub(one, token)


def expand_placeholders(token: str) -> str:
    return _expand_env_vars(token.replace("{python}", sys.executable))


def command_args_as_flags(spec_params: Dict[str, Any], args: Mapping[str, Any]) -> List[str]:
    """Render args as CLI flags using each property's ``flag`` (default ``--<name-with-dashes>``).

    Arrays repeat the flag per item; booleans add a bare flag when true; absent args are omitted.
    Objects are passed as one JSON string.
    """
    out: List[str] = []
    props = (spec_params or {}).get("properties") or {}
    for pname, prop in props.items():
        if pname not in args:
            continue
        val = args[pname]
        flag = (prop or {}).get("flag") or "--" + pname.replace("_", "-")
        if isinstance(val, bool):
            if val:
                out.append(flag)
        elif isinstance(val, list):
            for item in val:
                out.extend([flag, str(item)])
        elif isinstance(val, dict):
            out.extend([flag, json.dumps(val)])
        else:
            out.extend([flag, str(val)])
    return out


def make_command_handler(command: Sequence[str], parameters: Dict[str, Any],
                         args_mode: str = "flags", cwd: Optional[str] = None,
                         env: Optional[Dict[str, str]] = None,
                         timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS) -> ToolHandler:
    if args_mode not in ("flags", "stdin_json"):
        raise ValueError(f"args_mode must be 'flags' or 'stdin_json', not {args_mode!r}")
    base = [expand_placeholders(str(c)) for c in command]

    def handler(args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        argv = list(base)
        stdin = None
        if args_mode == "flags":
            argv += command_args_as_flags(parameters, args)
        else:
            stdin = json.dumps(args)
        run_env = dict(process_environment())
        run_env.update({k: expand_placeholders(str(v)) for k, v in (env or {}).items()})
        if ctx.quest_id:
            run_env.setdefault("QAR_TOOL_QUEST_ID", ctx.quest_id)
        if ctx.task_id:
            run_env.setdefault("QAR_TOOL_TASK_ID", ctx.task_id)
        if (ctx.meta or {}).get("run_id"):
            run_env.setdefault("QAR_TOOL_RUN_ID", str(ctx.meta["run_id"]))
        apply_requester_env(run_env, requester_from_meta({"user_id": ctx.user_id, **(ctx.meta or {})}))
        proc = subprocess.run(argv, input=stdin, capture_output=True, text=True,
                              cwd=expand_placeholders(cwd) if cwd else None, env=run_env,
                              timeout=timeout_seconds)
        out = (proc.stdout or "").strip()
        err = (proc.stderr or "").strip()
        data = None
        if out.startswith("{") or out.startswith("["):
            try:
                data = json.loads(out)
            except ValueError:
                data = None
        if proc.returncode != 0:
            return ToolResult(ok=False, text=(err or out or f"exit {proc.returncode}"), data=data)
        return ToolResult(ok=True, text=out or err or "done", data=data)

    return handler


# ---------------------------------------------------------------------------------------------
# Standard tools (every QAR user with Quest credentials gets these)
# ---------------------------------------------------------------------------------------------

# The standard tools talk to Quest through a client built from the environment. ``core`` may not
# import the runner that owns ``QuestClient``, so the factory (``env -> client``) is injected: the
# composition root registers ``runner.quest_client.quest_client_from_env`` here, and every tool
# spec also takes its own ``client_factory`` (what the tests pass).
_quest_client_factory: Optional[Callable[[Mapping[str, str]], Any]] = None


def set_quest_client_factory(factory: Optional[Callable[[Mapping[str, str]], Any]]) -> None:
    """Register the ``env -> Quest client`` factory the standard tools use by default (None clears it)."""
    global _quest_client_factory
    _quest_client_factory = factory


def quest_client_from_env(env: Mapping[str, str]):
    if _quest_client_factory is None:
        raise RuntimeError("no Quest client factory is registered; import quest_ai_runner (it "
                           "registers one) or call core.tools.set_quest_client_factory")
    return _quest_client_factory(env)


def quest_credentials_present(env: Mapping[str, str]) -> bool:
    return bool((env.get("QUEST_BASE_URL") or env.get("QUEST_API_URL")) and env.get("QUEST_API_KEY"))


#: Builders of the standard tools, by tool name. ``tools_quest_standard``, ``tools_quest_ops`` and
#: ``tools_quest_experiments`` register theirs here when imported (at the bottom of this module).
STANDARD_TOOL_BUILDERS: Dict[str, Callable[[Mapping[str, str]], Optional[ToolSpec]]] = {}
#: For a standard tool that needs configuration: what the planner is told while it is not configured (``None`` when it is).
STANDARD_TOOL_UNAVAILABLE: Dict[str, Callable[[Mapping[str, str]], Optional[UnavailableTool]]] = {}


# ---------------------------------------------------------------------------------------------
# The loader: standard tools + TOML-declared custom tools
# ---------------------------------------------------------------------------------------------

def env_flag(val: Optional[str], default: bool = True) -> bool:
    if val is None or val == "":
        return default
    return val.strip().lower() not in ("0", "false", "no", "off")


def load_handler(ref: str) -> ToolHandler:
    mod_name, _, fn_name = ref.partition(":")
    if not mod_name or not fn_name:
        raise ValueError(f"handler must be 'module:function', got {ref!r}")
    fn = getattr(importlib.import_module(mod_name), fn_name)
    if not callable(fn):
        raise ValueError(f"handler {ref!r} is not callable")
    return fn


def spec_from_toml_entry(entry: Mapping[str, Any]) -> ToolSpec:
    name = entry.get("name")
    if not name:
        raise ValueError("tool entry has no name")
    for key in ("description", "when_to_use", "when_not_to_use"):
        if not str(entry.get(key) or "").strip():
            raise ValueError(f"tool {name}: '{key}' is required (the model chooses by it)")
    params = dict(entry.get("parameters") or {"type": "object", "properties": {}})
    params.setdefault("type", "object")
    params.setdefault("properties", {})
    timeout = float(entry.get("timeout_seconds") or DEFAULT_TIMEOUT_SECONDS)
    if entry.get("command") and entry.get("handler"):
        raise ValueError(f"tool {name}: give 'command' or 'handler', not both")
    if entry.get("command"):
        cmd = entry["command"]
        if isinstance(cmd, str):
            cmd = shlex.split(cmd)
        handler = make_command_handler(cmd, params, args_mode=entry.get("args_mode", "flags"),
                                       cwd=entry.get("cwd"), env=entry.get("env"),
                                       timeout_seconds=timeout)
    elif entry.get("handler"):
        handler = load_handler(str(entry["handler"]))
    else:
        raise ValueError(f"tool {name}: needs 'command' or 'handler'")
    return ToolSpec(
        name=str(name), description=str(entry["description"]).strip(), handler=handler,
        when_to_use=str(entry["when_to_use"]).strip(),
        when_not_to_use=str(entry["when_not_to_use"]).strip(),
        parameters=params, mutates=bool(entry.get("mutates", True)), origin="custom",
        keywords=tuple(entry.get("keywords") or ()), defaults=dict(entry.get("defaults") or {}),
        context_args=dict(entry.get("context_args") or {}), timeout_seconds=timeout,
        workflow_step=(bool(entry["workflow_step"]) if entry.get("workflow_step") is not None else None))


def apply_standard_overrides(registry: ToolRegistry, overrides: Mapping[str, Any]) -> None:
    for name, ov in (overrides or {}).items():
        spec = registry.get(name)
        if spec is None or not isinstance(ov, Mapping):
            continue
        if ov.get("enabled") is False:
            registry.unregister(name)
            continue
        if ov.get("defaults"):
            spec.defaults = {**spec.defaults, **dict(ov["defaults"])}
        if ov.get("when_to_use_extra"):
            spec.when_to_use = f"{spec.when_to_use} {str(ov['when_to_use_extra']).strip()}".strip()
        if ov.get("when_not_to_use_extra"):
            spec.when_not_to_use = (f"{spec.when_not_to_use} "
                                    f"{str(ov['when_not_to_use_extra']).strip()}").strip()
        if ov.get("keywords"):
            spec.keywords = tuple(spec.keywords) + tuple(ov["keywords"])


def load_tools_file(path: str, registry: ToolRegistry) -> Dict[str, Any]:
    """Register every ``[[tool]]`` in one TOML file; return its ``[standard]`` override table."""
    import tomllib
    with open(os.path.expanduser(path), "rb") as fh:
        data = tomllib.load(fh)
    for entry in data.get("tool") or []:
        registry.register(spec_from_toml_entry(entry))
    return dict(data.get("standard") or {})


def build_tool_registry(env: Optional[Mapping[str, str]] = None) -> ToolRegistry:
    """The deployment's catalog from env. Never raises: a broken tools file is logged and skipped.

    ``QAR_STANDARD_TOOLS``  default on; ``0`` disables the standard tools.
    ``QAR_TOOLS_FILE``      ``:``-separated TOML files of custom tools and standard overrides.
    ``QAR_TOOL_LEDGER_DIR`` where each task's tool-call ledger lives (default: a folder in the
                            system temp directory); the brain and the deep worker must share it.
    ``QAR_METRIC_LEDGER_DIR`` the same for each run's metric results (``core/metric_results.py``).
    """
    env = process_environment() if env is None else env
    registry = ToolRegistry(ledger=FileToolLedger(env.get("QAR_TOOL_LEDGER_DIR") or None, name="qar-tool-ledger"),
                            metric_ledger=tools_metric_results.metric_ledger_from_env(env))
    if env_flag(env.get("QAR_STANDARD_TOOLS"), default=True):
        for name, builder in STANDARD_TOOL_BUILDERS.items():
            try:
                spec = builder(env)
            except Exception as e:
                log.warning("standard tool %s not available: %s", name, e)
                spec = None
            if spec is not None:
                registry.register(spec)
        for name, absent in STANDARD_TOOL_UNAVAILABLE.items():
            if registry.get(name) is None:
                entry = absent(env)
                if entry is not None:
                    registry.note_unavailable(entry)
    overrides: Dict[str, Any] = {}
    for path in [p for p in (env.get("QAR_TOOLS_FILE") or "").split(":") if p.strip()]:
        try:
            overrides.update(load_tools_file(path, registry))
        except Exception as e:
            log.error("QAR_TOOLS_FILE %s not loaded: %s", path, e)
    apply_standard_overrides(registry, overrides)
    return registry


# The standard Quest tools register themselves into STANDARD_TOOL_BUILDERS. They are imported last
# because those modules import this one; they are separate modules only to keep this one under the
# size limit and to give each operation area one place. Order here is the catalog order.
from .tools_quest_standard import (  # noqa: E402,F401  (kept importable from here)
    _ASKED_PARAM, _NEVER_CODE,
    _QUEST_ID_PARAM, _quest_id,
    )
from . import tools_quest_ops  # noqa: E402,F401
from . import tools_quest_work  # noqa: E402,F401
from . import tools_quest_intents  # noqa: E402,F401
from . import tools_repo_specs  # noqa: E402,F401
from . import tools_metric_results  # noqa: E402,F401
from . import tools_metric_values  # noqa: E402,F401
from . import tools_quest_experiments  # noqa: E402,F401
from . import tools_science  # noqa: E402,F401
