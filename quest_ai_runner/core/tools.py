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
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

log = logging.getLogger(__name__)

# At or below this many tools the planner simply sees them all: selection only earns its keep when
# the catalog is big enough that listing it every turn would crowd the prompt.
SHOW_ALL_AT_OR_BELOW = 8
DEFAULT_SELECT_K = 5
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
    ok: bool
    text: str = ""
    data: Any = None


ToolHandler = Callable[[Dict[str, Any], ToolContext], Any]


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


def tokenize(text: str) -> List[str]:
    return TOKEN_RE.findall((text or "").lower().replace("_", " "))


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

    def __init__(self, specs: Optional[Sequence[ToolSpec]] = None):
        self._specs: Dict[str, ToolSpec] = {}
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
        return (name_toks * 2 + kw_toks * 2 + tokenize(spec.description)
                + tokenize(spec.when_to_use))

    def search(self, query: str, k: int = DEFAULT_SELECT_K) -> List[ToolSpec]:
        """BM25 over the catalog; only tools that share a term with the query are returned."""
        q = set(tokenize(query))
        specs = self.all()
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
        scored = []
        for spec, d in zip(specs, docs):
            tf: Dict[str, int] = {}
            for t in d:
                tf[t] = tf.get(t, 0) + 1
            score = 0.0
            for t in q:
                if t not in tf:
                    continue
                idf = math.log(1 + (n - df[t] + 0.5) / (df[t] + 0.5))
                score += idf * tf[t] * (k1 + 1) / (tf[t] + k1 * (1 - b + b * len(d) / avgdl))
            if score > 0:
                scored.append((score, spec))
        scored.sort(key=lambda x: -x[0])
        return [s for _, s in scored[:k]]

    def select(self, query: str, k: int = DEFAULT_SELECT_K) -> List[ToolSpec]:
        """The tools to SHOW the planner for this request.

        A small catalog is shown whole (the model is better at relevance than a lexical score, and
        eight entries cost little). A large one is narrowed to the top ``k`` matches; the rest stay
        reachable through a ``{"tools": "<query>"}`` read.
        """
        if len(self._specs) <= SHOW_ALL_AT_OR_BELOW:
            return self.all()
        return self.search(query, k)

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

    def render_planner_block(self, query: str, k: int = DEFAULT_SELECT_K) -> str:
        shown = self.select(query, k)
        hidden = len(self._specs) - len(shown)
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
            "- A tool call already shown as succeeded in GATHERED is DONE: never call it again, "
            "answer with its receipt. If it failed, say so or try the right alternative once.")
        body = self.render_catalog(shown) if shown else "(no tool matched this request)"
        tail = (f"\n({hidden} more tool(s) not shown. Search them with a read spec "
                f"{{\"tools\": \"<what you need>\"}}.)" if hidden > 0 else "")
        return f"{head}\n{rules}\n{body}{tail}"

    def render_deep_block(self, query: str, k: int = DEFAULT_SELECT_K,
                          python: Optional[str] = None) -> str:
        """The tool section of a deep brief: relevant tools plus the exact command for each."""
        shown = self.select(query, k)
        if not shown:
            return ""
        py = python or sys.executable
        cmd = f"{shlex.quote(py)} -m quest_ai_runner.tools"
        parts = ["TOOLS YOU CAN CALL (use these instead of writing your own script for the same "
                 "job; each prints its result and exits 0 on success, 1 on failure):",
                 self.render_catalog(shown),
                 f"Call one:   {cmd} call <name> --args '<json object of args>'",
                 f"Find more:  {cmd} list --query '<what you need>'"]
        example = shown[0]
        example_args = {p: f"<{p}>" for p in example.required() if p not in example.auto_filled()}
        parts.append(f"Example:    {cmd} call {example.name} --args "
                     f"{shlex.quote(json.dumps(example_args))}")
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

        box: Dict[str, Any] = {}

        def run_handler():
            try:
                box["result"] = coerce_result(spec.handler(resolved, ctx))
            except Exception as e:  # a tool failure is a result, never a crash of the loop
                log.warning("tool %s raised: %s", name, e)
                box["result"] = ToolResult(ok=False, text=f"{name} failed: {e}")

        t = threading.Thread(target=run_handler, name=f"tool-{name}", daemon=True)
        t.start()
        t.join(spec.timeout_seconds)
        if t.is_alive():
            return ToolResult(ok=False, text=f"{name} timed out after {spec.timeout_seconds:.0f}s "
                                             f"(it may still complete; check before retrying)")
        result = box.get("result") or ToolResult(ok=False, text=f"{name} returned nothing")
        if len(result.text) > MAX_RESULT_CHARS:
            result.text = result.text[:MAX_RESULT_CHARS] + " …[truncated]"
        return result


# ---------------------------------------------------------------------------------------------
# Command-backed tools (the custom-tool path for anything that is not Python)
# ---------------------------------------------------------------------------------------------

def expand_placeholders(token: str) -> str:
    return os.path.expandvars(token.replace("{python}", sys.executable))


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
        run_env = dict(os.environ)
        run_env.update({k: expand_placeholders(str(v)) for k, v in (env or {}).items()})
        if ctx.quest_id:
            run_env.setdefault("QAR_TOOL_QUEST_ID", ctx.quest_id)
        if ctx.task_id:
            run_env.setdefault("QAR_TOOL_TASK_ID", ctx.task_id)
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

def quest_client_from_env(env: Mapping[str, str]):
    from ..runner.quest_client import QuestClient
    base = env.get("QUEST_BASE_URL") or env.get("QUEST_API_URL") or ""
    return QuestClient(base_url=base, api_key=env.get("QUEST_API_KEY") or "",
                       team_id=env.get("QUEST_TEAM_ID") or None)


def quest_credentials_present(env: Mapping[str, str]) -> bool:
    return bool((env.get("QUEST_BASE_URL") or env.get("QUEST_API_URL")) and env.get("QUEST_API_KEY"))


def send_quest_email_spec(env: Mapping[str, str], client_factory=None) -> ToolSpec:
    """The standard quest mailer: ``POST /api/quests/{id}/email`` through QuestClient."""
    factory = client_factory or (lambda: quest_client_from_env(env))

    def handler(args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        client = factory()
        result = client.send_quest_email(
            args["quest_id"], subject=args["subject"], body=args["body"],
            rep_id=args.get("rep_id"), task_id=args.get("task_id") or ctx.task_id,
            recipients=args.get("to") or None)
        who = ", ".join(args.get("to") or []) or "the quest's configured recipients"
        persona = (result or {}).get("persona") or "Quest AI"
        return ToolResult(ok=True, data=result,
                          text=f"Sent \"{args['subject']}\" to {who} as {persona} "
                               f"(quest {args['quest_id']}).")

    return ToolSpec(
        name="send_quest_email",
        handler=handler,
        description=("Send an email now through Quest's mailer for a quest: it carries the "
                     "quest's Reply-To (answers come back as quest notes), unsubscribe handling, "
                     "a record on the quest, and signs as the persona."),
        when_to_use=("The user asks to email or notify themselves, their team, or the quest's own "
                     "people, and the message can go out immediately with no human review."),
        when_not_to_use=("The message needs a person's review before sending (for example mail to "
                         "people outside the team, when a review tool is listed), or no quest is "
                         "known. Never use it to work around email being disabled on a quest."),
        parameters={
            "type": "object",
            "required": ["quest_id", "subject", "body"],
            "properties": {
                "quest_id": {"type": "string",
                             "description": "Quest the mail belongs to (defaults to this chat's quest)."},
                "subject": {"type": "string", "description": "Subject line."},
                "body": {"type": "string",
                         "description": "Full message body, plain text or markdown, final wording."},
                "to": {"type": "array", "items": {"type": "string"},
                       "description": ("One-off recipient override for this send only; omit to "
                                       "use the quest's own recipients.")},
                "rep_id": {"type": "string",
                           "description": "AI rep id whose name signs the mail (optional)."},
            },
        },
        mutates=True,
        origin="standard",
        keywords=("email", "mail", "send", "notify", "message", "inbox"),
        context_args={"quest_id": "quest_id"},
        timeout_seconds=45.0,
    )


def file_email_draft_spec(env: Mapping[str, str], client_factory=None) -> ToolSpec:
    """File an email draft into the user's Quest Approval Queue: ``POST /api/email-drafts``.

    Nothing is sent. A person reviews the draft in the Quest app and approving it is what sends
    it, through Quest's single send path. One draft per recipient: a draft is one message to one
    person, so several outside readers means several calls.
    """
    factory = client_factory or (lambda: quest_client_from_env(env))

    def handler(args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        recipient = str(args.get("to") or "").strip()
        if not recipient:
            return ToolResult(ok=False, text="No recipient was given, so no draft was filed.")
        client = factory()
        result = client.create_email_draft(
            recipient=recipient, subject=args["subject"], body=args["body"],
            cc=args.get("cc") or None, task_id=args.get("task_id") or ctx.task_id)
        draft_id = (result or {}).get("id")
        if not draft_id:
            return ToolResult(ok=False, data=result,
                              text="The draft was not filed: Quest did not return a draft id.")
        return ToolResult(ok=True, data=result,
                          text=(f"Filed a draft \"{args['subject']}\" to {recipient} in the "
                                f"Approval Queue (draft {draft_id}). NOT sent: it goes out only "
                                f"after the person approves it in the Quest app."))

    return ToolSpec(
        name="file_email_draft",
        handler=handler,
        description=("File an email draft into the user's Approval Queue in the Quest app. "
                     "Nothing is sent: the person reviews and edits it there, and approving it "
                     "sends it through Quest's mailer with unsubscribe handling."),
        when_to_use=("The user asks for an email to someone outside their own team (a donor, "
                     "partner, applicant, lead, any one-off contact), or asks for a draft to "
                     "review, or the message is one a person should read before it goes out."),
        when_not_to_use=("An update to the quest's own people that can go out immediately: use "
                         "send_quest_email. Never to bulk-mail a list. Never tell the user the "
                         "email was sent: it is a draft awaiting their approval."),
        parameters={
            "type": "object",
            "required": ["to", "subject", "body"],
            "properties": {
                "to": {"type": "string", "description": "The one recipient email address."},
                "subject": {"type": "string", "description": "Subject line."},
                "body": {"type": "string",
                         "description": "Full message body, plain text, ready to send. No em dashes."},
                "cc": {"type": "array", "items": {"type": "string"},
                       "description": ("Extra CC addresses. The account's standing Always CC is "
                                       "added automatically; do not add it yourself.")},
            },
        },
        mutates=True,
        origin="standard",
        keywords=("email", "draft", "review", "approval", "queue", "outreach", "external"),
        timeout_seconds=45.0,
    )


# ---------------------------------------------------------------------------------------------
# Quest data tools: ONE tool per quest field, plus goals, strategies and habits
# ---------------------------------------------------------------------------------------------
#
# WHY ONE TOOL PER FIELD (2026-10-11). The single ``update_quest_fields`` tool that came before
# took any of seven fields in one ``fields`` object, so its description could only say WHICH fields
# exist, never how to write each one, and AI-written vision statements, current states and
# measurable outcomes read as generic. Each tool below writes exactly one field (or adds one goal,
# strategy or habit) and its description carries that field's own writing standard from
# ``core.quest_field_instructions``, so the model reads the standard at the moment it chooses the
# write. They remain the ONLY way an AI changes quest data: never generated code, a file edit or a
# coding agent (Joshua, 2026-10-01: "AI generated code should never be used for quest field
# updates, qar has a specific tool for that").
#
# GOVERNANCE IS THE BACKEND'S. Every field write carries ``actor="ai"`` and the structured
# ``userRequested`` verdict, and Quest's AI field-write gate decides: a write the person did not ask
# for, or any write on a quest whose autopilot is off, becomes an ask for the owner instead of an
# edit, which these tools report as NOT applied. Goals and habits are new items rather than the
# person's own words, so the backend does not gate them; these tools refuse to create one the
# person did not ask for and say to suggest it instead.

#: Field name on the quest -> the tool that writes it. Tool names say the field in plain words.
QUEST_FIELD_TOOLS: Dict[str, str] = {
    "outcome": "update_quest_outcome",
    "purpose": "update_quest_purpose",
    "current_state": "update_quest_current_state",
    "preferences": "update_quest_preferences",
    "acceptance_criteria": "update_quest_acceptance_criteria",
    "scope": "update_quest_scope",
    "quest_goal": "update_quest_goal_statement",
    "quest_completion_criteria": "update_quest_completion_criteria",
}

#: Fields the backend's AI field-write gate does not cover, so the tool itself holds the line: a
#: write the person did not ask for is not sent at all.
UNGATED_QUEST_FIELDS = frozenset({"scope"})

_NEVER_CODE = ("This tool is the ONLY way this changes: never write code, edit a file, or run a "
               "coding agent to do it.")
_SUGGEST_INSTEAD = ("Do not use it off the back of small work inside the quest (a task, a "
                    "progress note, one new goal): suggest the change in your reply instead.")

_ASKED_PARAM = {
    "type": "boolean",
    "description": ("True ONLY when the user's own message explicitly asked for THIS change. It "
                    "is a verdict you already hold from what they said, never something to read "
                    "out of your own wording. False means it is offered for the owner to approve "
                    "(or, for an item, not created) instead of applied."),
}
_QUEST_ID_PARAM = {"type": "string", "description": "Quest to change (defaults to this chat's quest)."}


def _with_standard(description: str, key: str, period: Optional[str] = None) -> str:
    from .quest_field_instructions import (UNIVERSAL_RULES, field_instructions_text,
                                           goal_instructions_text)
    standard = goal_instructions_text(period) if key == "goal" else field_instructions_text(key)
    always = "\n".join(f"- {r}" for r in UNIVERSAL_RULES)
    return f"{description}\n\nHOW TO WRITE IT:\n{standard}\nAlways:\n{always}"


def _not_applied_text(what: str, quest_id: str, result: Mapping[str, Any]) -> str:
    reason = result.get("reason") or result.get("message") or "it needs the owner's approval"
    return (f"NOT applied: {what} on quest {quest_id} was not changed ({reason}). It is now an ask "
            f"for the owner to approve, so the quest reads exactly as it did.")


def _quest_id(args: Mapping[str, Any], ctx: ToolContext) -> Optional[str]:
    return str(args.get("quest_id") or ctx.quest_id or "") or None


def quest_field_tool_spec(field_name: str, env: Mapping[str, str],
                          client_factory=None) -> ToolSpec:
    """The tool that writes ONE quest field through ``PATCH /api/quests/{id}/field``."""
    from .quest_field_instructions import QUEST_FIELD_INSTRUCTIONS
    factory = client_factory or (lambda: quest_client_from_env(env))
    spec_text = QUEST_FIELD_INSTRUCTIONS[field_name]

    def handler(args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        quest_id = _quest_id(args, ctx)
        if not quest_id:
            return ToolResult(ok=False, text="No quest was named, so there is nothing to update.")
        value = args.get("value")
        if not isinstance(value, str) or not value.strip():
            return ToolResult(ok=False, text=f"No new {spec_text.label} was given, so nothing was "
                                             f"written.")
        requested = bool(args.get("user_asked_for_this_field"))
        if field_name in UNGATED_QUEST_FIELDS and not requested:
            return ToolResult(ok=False, text=(f"Not written: the user did not ask to change the "
                                              f"{spec_text.label}. Suggest it in your reply instead."))
        result = factory().edit_quest_field(quest_id, {field_name: value.strip()},
                                            actor="ai", user_requested=requested)
        if not result:
            return ToolResult(ok=False, data=result,
                              text=f"Nothing was written: the update of {field_name} on quest "
                                   f"{quest_id} failed.")
        if isinstance(result, dict) and result.get("applied") is False:
            return ToolResult(ok=True, data=result,
                              text=_not_applied_text(field_name, quest_id, result))
        return ToolResult(ok=True, data=result, text=f"Updated {field_name} on quest {quest_id}.")

    return ToolSpec(
        name=QUEST_FIELD_TOOLS[field_name],
        handler=handler,
        description=_with_standard(
            f"Write a quest's {spec_text.label} through Quest's governed field-update route.",
            field_name),
        when_to_use=(f"The user asked to set or change their quest's {spec_text.label.lower()}. "
                     + _NEVER_CODE),
        when_not_to_use=("The user did not ask for THIS field to change in this message. "
                         + _SUGGEST_INSTEAD + " Not for code or file changes of any kind."),
        parameters={
            "type": "object",
            "required": ["value"],
            "properties": {
                "quest_id": _QUEST_ID_PARAM,
                "value": {"type": "string",
                          "description": (f"The FULL new {spec_text.label}, written to the "
                                          f"standard in this tool's description. Merge new "
                                          f"information into the current value rather than "
                                          f"replacing it with a fragment.")},
                "user_asked_for_this_field": _ASKED_PARAM,
            },
        },
        mutates=True,
        origin="standard",
        keywords=("quest", "field", "update", "change", "set", field_name.replace("_", " "),
                  *spec_text.label.lower().replace("(", " ").replace(")", " ").split()),
        context_args={"quest_id": "quest_id"},
        timeout_seconds=45.0,
    )


def add_measurable_outcome_spec(env: Mapping[str, str], client_factory=None) -> ToolSpec:
    """Add ONE item to a quest's measurable outcomes, keeping every existing item untouched.

    The field is a whole list on the API, so the tool reads the current list and writes it back
    with the new item appended (the backend keeps every progress field and id the request carries
    or leaves out). The write passes the same AI field-write gate as any other quest field.
    """
    factory = client_factory or (lambda: quest_client_from_env(env))

    def handler(args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        quest_id = _quest_id(args, ctx)
        if not quest_id:
            return ToolResult(ok=False, text="No quest was named, so there is nothing to update.")
        text = str(args.get("text") or "").strip()
        if not text:
            return ToolResult(ok=False, text="No measurable outcome text was given, so nothing "
                                             "was written.")
        client = factory()
        state = client.get_my_quest(quest_id)
        quest = (state.get("state") if isinstance(state, dict) else None) or state or {}
        if not quest:
            return ToolResult(ok=False, text=f"Nothing was written: quest {quest_id} could not be "
                                             f"read, so its current outcomes are unknown.")
        current = [o for o in (quest.get("measurable_outcomes") or quest.get("measurableOutcomes")
                               or []) if isinstance(o, dict)]
        if any(str(o.get("text") or "").strip().lower() == text.lower() for o in current):
            return ToolResult(ok=True, text=f"Not added: quest {quest_id} already has the "
                                            f"measurable outcome {text!r}.")
        item: Dict[str, Any] = {"text": text, "completed": False}
        for key in ("target_value", "target_unit", "start_value"):
            if args.get(key) not in (None, ""):
                item[key] = args[key]
        result = client.edit_quest_field(quest_id, {"measurable_outcomes": current + [item]},
                                         actor="ai",
                                         user_requested=bool(args.get("user_asked_for_this_field")))
        if not result:
            return ToolResult(ok=False, text=f"Nothing was written: adding the measurable outcome "
                                             f"to quest {quest_id} failed.")
        if isinstance(result, dict) and result.get("applied") is False:
            return ToolResult(ok=True, data=result,
                              text=_not_applied_text("measurable_outcomes", quest_id, result))
        return ToolResult(ok=True, data=result,
                          text=f"Added the measurable outcome {text!r} to quest {quest_id}.")

    return ToolSpec(
        name="add_quest_measurable_outcome",
        handler=handler,
        description=_with_standard(
            "Add one measurable outcome to a quest's checklist, leaving the existing items as "
            "they are.", "measurable_outcomes"),
        when_to_use=("The user asked to add a measurable outcome (a success metric) to their "
                     "quest, or the quest is being specified for the first time. " + _NEVER_CODE),
        when_not_to_use=("A new goal or task: give THAT goal its own criteria instead. Rewording "
                         "or deleting existing outcomes. " + _SUGGEST_INSTEAD),
        parameters={
            "type": "object",
            "required": ["text"],
            "properties": {
                "quest_id": _QUEST_ID_PARAM,
                "text": {"type": "string",
                         "description": "The outcome, one countable metric with its target."},
                "target_value": {"type": "number", "description": "The target number, e.g. 1000."},
                "target_unit": {"type": "string", "description": "Its unit, e.g. 'subscribers'."},
                "start_value": {"type": "number", "description": "Baseline, when known."},
                "user_asked_for_this_field": _ASKED_PARAM,
            },
        },
        mutates=True,
        origin="standard",
        keywords=("quest", "measurable", "outcome", "metric", "success", "target", "kpi", "add"),
        context_args={"quest_id": "quest_id"},
        timeout_seconds=45.0,
    )


def create_quest_goal_spec(env: Mapping[str, str], client_factory=None) -> ToolSpec:
    """Create one Goal on a quest's plan, scoped to a day, week, month, quarter or year."""
    factory = client_factory or (lambda: quest_client_from_env(env))

    def handler(args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        quest_id = _quest_id(args, ctx)
        title = str(args.get("title") or "").strip()
        period = str(args.get("period") or "").strip()
        if not quest_id or not title or not period:
            return ToolResult(ok=False, text="A goal needs a quest, a title and a period; nothing "
                                             "was created.")
        if not args.get("user_asked_for_this_field"):
            return ToolResult(ok=False, text=(f"Not created: the user did not ask for the goal "
                                              f"{title!r}. Suggest it in your reply instead."))
        kwargs = {k: args[k] for k in ("description", "criteria", "target_value", "target_unit",
                                       "parent_goal_id") if args.get(k) not in (None, "")}
        try:
            goal = factory().create_goal(title, period=period, quest_id=quest_id, **kwargs)
        except Exception as e:  # QuestApiError / QuestNotConfigured: report, never raise
            return ToolResult(ok=False, text=f"Nothing was created: {e}")
        if not goal:
            return ToolResult(ok=False, text=f"Nothing was created: the goal {title!r} was refused.")
        return ToolResult(ok=True, data=goal,
                          text=f"Created the {period} goal {title!r} on quest {quest_id}.")

    return ToolSpec(
        name="create_quest_goal",
        handler=handler,
        description=_with_standard(
            "Create one goal on a quest's plan for one period (day, week, month, quarter or "
            "year). Follow the nuance for the goal's period.", "goal"),
        when_to_use=("The user asked to add a goal (or a day's, week's, month's, quarter's or "
                     "year's goal) to their quest. " + _NEVER_CODE),
        when_not_to_use=("Background AI work for later (that is a task, not a goal). A goal the "
                         "user did not ask for: suggest it instead. Changing the quest's own "
                         "fields or measurable outcomes."),
        parameters={
            "type": "object",
            "required": ["title", "period"],
            "properties": {
                "quest_id": _QUEST_ID_PARAM,
                "title": {"type": "string", "description": "Verb-first goal title."},
                "period": {"type": "string",
                           "description": ("The goal's period: YYYY-MM-DD (day), YYYY_W## (week), "
                                           "YYYY_MM (month), YYYY_Q# (quarter) or YYYY (year).")},
                "description": {"type": "string", "description": "Optional detail."},
                "criteria": {"type": "string",
                             "description": "How anyone can check it is done at period end."},
                "target_value": {"type": "number", "description": "Target number, when it has one."},
                "target_unit": {"type": "string", "description": "Unit of the target."},
                "parent_goal_id": {"type": "string",
                                   "description": "The longer-period goal this one serves."},
                "user_asked_for_this_field": _ASKED_PARAM,
            },
        },
        mutates=True,
        origin="standard",
        keywords=("goal", "plan", "day", "week", "month", "quarter", "year", "add", "create",
                  "today", "this week"),
        context_args={"quest_id": "quest_id"},
        timeout_seconds=45.0,
    )


def add_quest_strategy_spec(env: Mapping[str, str], client_factory=None) -> ToolSpec:
    """Add one strategy to a quest through ``POST /api/quests/{id}/strategies/add`` as the AI."""
    factory = client_factory or (lambda: quest_client_from_env(env))

    def handler(args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        quest_id = _quest_id(args, ctx)
        title = str(args.get("title") or "").strip()
        if not quest_id or not title:
            return ToolResult(ok=False, text="A strategy needs a quest and a title; nothing was "
                                             "added.")
        result = factory().add_quest_strategy(
            quest_id, title, str(args.get("description") or "").strip(),
            actor="ai", user_requested=bool(args.get("user_asked_for_this_field")))
        if not result:
            return ToolResult(ok=False, text=f"Nothing was added: the strategy {title!r} on quest "
                                             f"{quest_id} failed.")
        if isinstance(result, dict) and result.get("applied") is False:
            return ToolResult(ok=True, data=result,
                              text=_not_applied_text(f"the strategy {title!r}", quest_id, result))
        return ToolResult(ok=True, data=result,
                          text=f"Added the strategy {title!r} to quest {quest_id}.")

    return ToolSpec(
        name="add_quest_strategy",
        handler=handler,
        description=_with_standard("Add one strategy (an approach) to a quest.", "strategy"),
        when_to_use="The user asked to add a strategy or approach to their quest. " + _NEVER_CODE,
        when_not_to_use=("A single goal or task (use create_quest_goal). A strategy the user did "
                         "not ask for: suggest it instead."),
        parameters={
            "type": "object",
            "required": ["title"],
            "properties": {
                "quest_id": _QUEST_ID_PARAM,
                "title": {"type": "string", "description": "3 to 8 words naming the approach."},
                "description": {"type": "string",
                                "description": "What it is, why it fits this person, what it "
                                               "implies each week."},
                "user_asked_for_this_field": _ASKED_PARAM,
            },
        },
        mutates=True,
        origin="standard",
        keywords=("strategy", "approach", "method", "quest", "add"),
        context_args={"quest_id": "quest_id"},
        timeout_seconds=45.0,
    )


def create_quest_habit_spec(env: Mapping[str, str], client_factory=None) -> ToolSpec:
    """Create one habit under a quest strategy through ``POST /api/planning/habits``."""
    factory = client_factory or (lambda: quest_client_from_env(env))

    def handler(args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        quest_id = _quest_id(args, ctx)
        name = str(args.get("name") or "").strip()
        if not quest_id or not name:
            return ToolResult(ok=False, text="A habit needs a quest and a name; nothing was "
                                             "created.")
        if not args.get("user_asked_for_this_field"):
            return ToolResult(ok=False, text=(f"Not created: the user did not ask for the habit "
                                              f"{name!r}. Suggest it in your reply instead."))
        client = factory()
        strategy_id = str(args.get("strategy_id") or "").strip()
        if not strategy_id:
            state = client.get_my_quest(quest_id)
            quest = (state.get("state") if isinstance(state, dict) else None) or state or {}
            strategies = [s for s in (quest.get("strategies") or []) if isinstance(s, dict)]
            accepted = [s for s in strategies if s.get("accepted")] or strategies
            strategy_id = str((accepted[0] if accepted else {}).get("id") or "")
        if not strategy_id:
            return ToolResult(ok=False, text=(f"Nothing was created: quest {quest_id} has no "
                                              f"strategy to put the habit under. Add a strategy "
                                              f"first (add_quest_strategy)."))
        fields = {k: args[k] for k in ("description", "days", "time", "duration", "start_date",
                                       "linked_goal_id") if args.get(k) not in (None, "", [])}
        try:
            habit = client.create_habit(quest_id, strategy_id, name,
                                        frequency=str(args.get("frequency") or "daily"), **fields)
        except Exception as e:  # QuestApiError / QuestNotConfigured: report, never raise
            return ToolResult(ok=False, text=f"Nothing was created: {e}")
        if not habit:
            return ToolResult(ok=False, text=f"Nothing was created: the habit {name!r} was refused.")
        return ToolResult(ok=True, data=habit, text=f"Created the habit {name!r} on quest {quest_id}.")

    return ToolSpec(
        name="create_quest_habit",
        handler=handler,
        description=_with_standard("Create one recurring habit on a quest, under one of its "
                                   "strategies.", "habit"),
        when_to_use="The user asked to add a habit or routine to their quest. " + _NEVER_CODE,
        when_not_to_use=("Logging that a habit was done today (that is a habit entry, not a new "
                         "habit). A one-off action (use create_quest_goal). A habit the user did "
                         "not ask for: suggest it instead."),
        parameters={
            "type": "object",
            "required": ["name"],
            "properties": {
                "quest_id": _QUEST_ID_PARAM,
                "name": {"type": "string", "description": "Verb-first name with the minimum dose."},
                "frequency": {"type": "string",
                              "enum": ["daily", "weekly", "monthly", "quarterly", "yearly"],
                              "description": "How often (default daily)."},
                "days": {"type": "array", "items": {"type": "string"},
                         "description": "For weekly: day names like ['Mon','Wed','Fri']."},
                "time": {"type": "string", "description": "Time of day, HH:MM."},
                "duration": {"type": "integer", "description": "Minutes per session."},
                "description": {"type": "string", "description": "Optional detail."},
                "strategy_id": {"type": "string",
                                "description": "Strategy it serves (defaults to the quest's first "
                                               "accepted strategy)."},
                "linked_goal_id": {"type": "string",
                                   "description": "Goal its entries count toward."},
                "start_date": {"type": "string", "description": "YYYY-MM-DD (default today)."},
                "user_asked_for_this_field": _ASKED_PARAM,
            },
        },
        mutates=True,
        origin="standard",
        keywords=("habit", "routine", "recurring", "daily", "weekly", "quest", "add", "create"),
        context_args={"quest_id": "quest_id"},
        timeout_seconds=45.0,
    )


def _with_credentials(build: Callable[[Mapping[str, str]], ToolSpec]):
    return lambda env: build(env) if quest_credentials_present(env) else None


STANDARD_TOOL_BUILDERS: Dict[str, Callable[[Mapping[str, str]], Optional[ToolSpec]]] = {
    "send_quest_email": _with_credentials(send_quest_email_spec),
    "file_email_draft": _with_credentials(file_email_draft_spec),
    **{tool: _with_credentials(lambda env, f=field_name: quest_field_tool_spec(f, env))
       for field_name, tool in QUEST_FIELD_TOOLS.items()},
    "add_quest_measurable_outcome": _with_credentials(add_measurable_outcome_spec),
    "create_quest_goal": _with_credentials(create_quest_goal_spec),
    "add_quest_strategy": _with_credentials(add_quest_strategy_spec),
    "create_quest_habit": _with_credentials(create_quest_habit_spec),
}

#: Every standard tool that writes quest data, for guards and briefs that must name them.
QUEST_DATA_TOOLS: Tuple[str, ...] = (*QUEST_FIELD_TOOLS.values(), "add_quest_measurable_outcome",
                                     "create_quest_goal", "add_quest_strategy",
                                     "create_quest_habit")


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
        context_args=dict(entry.get("context_args") or {}), timeout_seconds=timeout)


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
    """
    env = os.environ if env is None else env
    registry = ToolRegistry()
    if env_flag(env.get("QAR_STANDARD_TOOLS"), default=True):
        for name, builder in STANDARD_TOOL_BUILDERS.items():
            try:
                spec = builder(env)
            except Exception as e:
                log.warning("standard tool %s not available: %s", name, e)
                spec = None
            if spec is not None:
                registry.register(spec)
    overrides: Dict[str, Any] = {}
    for path in [p for p in (env.get("QAR_TOOLS_FILE") or "").split(":") if p.strip()]:
        try:
            overrides.update(load_tools_file(path, registry))
        except Exception as e:
            log.error("QAR_TOOLS_FILE %s not loaded: %s", path, e)
    apply_standard_overrides(registry, overrides)
    return registry
