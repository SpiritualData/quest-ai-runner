"""Quest API subcommands: `quest`, `send`, `reconcile-plan`, `create-goal` and `sync-quest-folder`.

Split out of ``cli.py`` by definition; that module re-exports every name here.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import date

from .config import ConfigFileError, load_quest_client
from .cli_context import select_card_ids_for_text
from .cli_config import _ask_classifier, _goal_judge, _model_provider_from_env


# Method-name prefixes that only READ from Quest. Everything else needs an explicit --write, so a
# typo or an over-eager agent cannot delete a goal while "just checking" one. A prefix rule rather
# than a hand-kept allowlist: QuestClient grows read methods regularly, and a list that drifts
# would start refusing perfectly safe lookups (the failure that sends people back to ad-hoc
# scripts). Write methods are the smaller, more deliberate set.
_QUEST_READ_PREFIXES = ("get_", "list_", "is_", "search_", "discover_", "owning_", "goals_from_")


_QUEST_READ_EXACT = frozenset({"whoami", "assignee_id"})


def _quest_method_is_read_only(name: str) -> bool:
    return name in _QUEST_READ_EXACT or name.startswith(_QUEST_READ_PREFIXES)


def _quest_callable_methods() -> List[str]:
    from .runner.quest_client import QuestClient

    return sorted(n for n in dir(QuestClient)
                  if not n.startswith("_") and callable(getattr(QuestClient, n, None)))


def operation_registry():
    """The described operations (the same registry a run's brief draws from), built without
    credentials: only the descriptions are read here, no handler runs."""
    from .core.tools import build_tool_registry

    env = dict(os.environ)
    env.setdefault("QUEST_BASE_URL", "describe-only")
    env.setdefault("QUEST_API_KEY", "describe-only")
    return build_tool_registry(env)


def first_sentence(text: str, limit: int = 110) -> str:
    one_line = " ".join((text or "").split())
    cut = one_line.split(". ")[0].rstrip(".")
    return cut if len(cut) <= limit else cut[:limit - 1].rstrip() + "..."


def describe_quest_method(name: str) -> int:
    """``quest --describe NAME``: how to call one operation, from the one place it is defined.

    A described operation prints its registry entry (purpose, when to use it, arguments) and the
    shell command; any other client method prints its signature and docstring. An unknown name gets
    the closest real names back, never a bare failure.
    """
    import difflib
    import inspect

    spec = operation_registry().get(name)
    if spec is not None:
        from .core.tools import ToolRegistry
        print(ToolRegistry.render_one(spec))
        print(f"Call:  python -m quest_ai_runner.tools call {spec.name} --kw ARG=VALUE ...")
        return 0
    from .runner.quest_client import QuestClient

    method = getattr(QuestClient, name, None) if not name.startswith("_") else None
    if method is None or not callable(method):
        close = difflib.get_close_matches(name, _quest_callable_methods(), n=4, cutoff=0.5)
        print(f"no such QuestClient method {name!r}." +
              (f" Did you mean: {', '.join(close)}?" if close else " Try `quest-ai-runner quest --list`."),
              file=sys.stderr)
        return 2
    kind = "read" if _quest_method_is_read_only(name) else "WRITE (needs --write)"
    print(f"{name}{inspect.signature(method)}  [{kind}]\n{inspect.getdoc(method) or '(no description)'}"
          .replace("(self, ", "(").replace("(self)", "()"))
    return 0


def _parse_kw(pairs: List[str]) -> Dict[str, Any]:
    """``NAME=VALUE`` pairs into kwargs, VALUE as JSON when it parses as JSON, else a string.

    So ``--kw status=queued`` passes the string and ``--kw limit=5`` / ``--kw fields='{"a":1}'``
    pass an int and a dict, with no per-argument type table to keep in sync with the client.
    """
    out: Dict[str, Any] = {}
    for pair in pairs:
        name, sep, raw = pair.partition("=")
        if not sep:
            raise ValueError(f"--kw {pair!r} is not NAME=VALUE")
        try:
            out[name.strip()] = json.loads(raw)
        except ValueError:
            out[name.strip()] = raw
    return out


def _run_quest_command(args) -> int:
    """``quest-ai-runner quest <method> [args] [--kw k=v]`` — the Quest API from a shell.

    The zero-Python path for the lookups that used to be written as throwaway scripts (what is
    this task's status, what is this quest's autopilot config, what notes does this goal have).
    Dispatching by method name rather than adding a subcommand per endpoint keeps this in step
    with QuestClient for free: a method added there is callable here the day it lands.
    """
    from .runner.quest_client import QuestApiError, QuestNotConfigured

    if getattr(args, "describe", None):
        return describe_quest_method(args.describe)
    if args.list_methods:
        from .runner.quest_client import QuestClient
        described = operation_registry()
        for name in _quest_callable_methods():
            spec = described.get(name)
            summary = (first_sentence(spec.description) if spec is not None
                       else first_sentence(getattr(getattr(QuestClient, name), "__doc__", "") or ""))
            print(f"{'read ' if _quest_method_is_read_only(name) else 'WRITE'}  {name}"
                  f"{'  - ' + summary if summary else ''}")
        return 0
    if not args.method:
        print("usage: quest-ai-runner quest <method> [args...] [--kw NAME=VALUE]\n"
              "       quest-ai-runner quest --list\n"
              "       quest-ai-runner quest --describe <method>", file=sys.stderr)
        return 2

    method_name = args.method
    if not _quest_method_is_read_only(method_name) and not args.write:
        print(f"{method_name!r} can modify Quest; pass --write to allow it "
              f"(`quest-ai-runner quest --list` marks which methods write).", file=sys.stderr)
        return 2

    try:
        kwargs = _parse_kw(args.kw)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 2

    try:
        # The lightweight client path, not _config_from_env: a lookup must not pay for building a
        # model provider and a context store, nor require their optional dependencies.
        client = load_quest_client(getattr(args, "config", None))
    except (QuestNotConfigured, ConfigFileError) as e:
        print(str(e), file=sys.stderr)
        return 1

    method = getattr(client, method_name, None)
    if method is None or method_name.startswith("_") or not callable(method):
        return describe_quest_method(method_name) or 2

    try:
        result = method(*args.args, **kwargs)
    except TypeError as e:  # wrong arity/keyword — the method's own signature is the help text
        import inspect
        print(f"{method_name}{inspect.signature(method)}\n{e}\n"
              f"(`quest-ai-runner quest --describe {method_name}` explains each argument)",
              file=sys.stderr)
        return 2
    except (QuestApiError, QuestNotConfigured) as e:
        print(str(e), file=sys.stderr)
        return 1

    print(json.dumps(result, indent=None if args.compact else 2, default=str))
    if not result and not _quest_method_is_read_only(method_name):
        # Most client writes swallow a refusal and hand back {} / [] / False (logging the reason),
        # which read as success on a shell. An empty answer from a write is a write that did not
        # happen, so say so and exit non-zero instead of letting a run report it as done.
        print(f"{method_name} returned nothing: Quest refused it or it failed (the reason is in the "
              f"log line above). Treat it as NOT done; "
              f"`quest-ai-runner quest --describe {method_name}` shows its arguments.", file=sys.stderr)
        return 1
    return 0


def _cmd_send(args, log) -> int:
    """Run the `send` subcommand; returns the process exit code."""
    from .runner.quest_client import QuestClient, QuestApiError, QuestNotConfigured
    base_url = os.getenv("QUEST_BASE_URL", "")
    api_key = os.getenv("QUEST_API_KEY", "")
    team_id = args.team_id or os.getenv("QUEST_TEAM_ID", "")
    if not base_url or not api_key:
        log.error("QUEST_BASE_URL and QUEST_API_KEY must be set")
        return 1
    client = QuestClient(base_url, api_key, team_id=team_id)

    # Card ids come from two sources: explicit --card flags, or (when none were given and
    # auto-selection isn't disabled) a best-effort card search over the task text, reusing
    # the same selection logic as `search-context`. Keyword/IDF only by default (no model
    # call) so `send` stays instant and offline; selection failure never blocks the send.
    card_ids: List[str] = list(args.card_ids) if getattr(args, "card_ids", None) else []
    auto_result = None
    if not card_ids and not getattr(args, "no_auto_cards", False):
        try:
            auto_result = select_card_ids_for_text(args.text)
            if auto_result and auto_result.card_ids:
                card_ids = list(auto_result.card_ids)
        except Exception:  # noqa: BLE001 -- auto card selection must never block send
            auto_result = None
            card_ids = []

    if card_ids:
        topic_names = [
            (m.get("title") or m.get("id"))
            for m in (getattr(auto_result, "card_metadata", None) or [])
        ] if auto_result is not None else []
        label = ", ".join(str(t) for t in topic_names) if topic_names else ", ".join(card_ids)
        print(f"Attached {len(card_ids)} context card(s): {label}")

    try:
        task = client.create_task(
            args.text,
            team_id=args.team_id,
            goal_id=args.goal_id,
            scheduled_at=args.scheduled_at,
            card_ids=card_ids,
        )
    except (QuestApiError, QuestNotConfigured) as e:
        log.error("failed to enqueue task: %s", e)
        print(f"Could not queue the task: {e}")
        return 1
    if not (task.get("id") or task.get("task_id")):
        # The API answered without a task id: treat as a failure, never ack a task that
        # may not exist (an ack for an unenqueued task is a promise nothing will fulfill).
        log.error("task creation returned no id: %s", task)
        print("Could not queue the task: the Quest API returned no task id.")
        return 1
    # Immediate ack: fire a cheap one-sentence restatement so the user sees feedback
    # right away, then exit.  The queued task runs in the background via the poller.
    try:
        provider = _model_provider_from_env()
        from .core.model_registry import ModelRegistry
        ack_model = ModelRegistry(provider).resolve_tier("fast")
        ack_prompt = (
            "Write ONE sentence (max 20 words) that restates the following "
            "request in your own words and says you are looking into it. "
            "Do NOT use em dashes (--). Be natural and brief.\n\n"
            f"Request: {args.text[:300]}"
        )
        ack = provider.answer([{"role": "user", "content": ack_prompt}], model=ack_model)
        if ack and ack.strip():
            print(ack.strip())
            return 0
    except Exception:  # noqa: BLE001 — ack failure is non-fatal
        pass
    task_id = task.get("id") or task.get("task_id") or "?"
    print(f"Queued: {args.text[:80]}  ({task_id})")
    return 0


def _cmd_reconcile_plan(args, log) -> int:
    """Run the `reconcile-plan` subcommand; returns the process exit code."""
    from .runner.quest_client import QuestClient, QuestApiError, QuestNotConfigured
    from .runner.plan_reconcile import reconcile_quest
    base_url = os.getenv("QUEST_BASE_URL", "")
    api_key = os.getenv("QUEST_API_KEY", "")
    if not base_url or not api_key:
        log.error("QUEST_BASE_URL and QUEST_API_KEY must be set")
        return 1
    client = QuestClient(base_url, api_key)
    today = date.fromisoformat(args.today) if args.today else date.today()
    classify = _ask_classifier(args.quest_id, client)
    # AI handoff is on by default; QAR_RECONCILE_HANDOFF=0 turns it off. The assignee's own AI-rep
    # preference still decides what a handoff becomes (auto, ask or never).
    judge_goal = _goal_judge(args.quest_id, client) if os.getenv("QAR_RECONCILE_HANDOFF", "1") != "0" else None
    try:
        notify_stall = {"user_id": os.getenv("QAR_STALL_NOTIFY_USER_ID", ""),
                        "team_id": os.getenv("QAR_STALL_NOTIFY_TEAM_ID", "")}
        actions = reconcile_quest(client, args.quest_id, today, write=args.write, classify=classify,
                                  judge_goal=judge_goal, notify_stall=notify_stall)
    except (QuestApiError, QuestNotConfigured) as e:
        log.error(f"reconcile-plan failed: {e}")
        return 1
    for action in actions:
        print(json.dumps(action, ensure_ascii=False))
    print(f"{len(actions)} change(s) {'applied' if args.write else 'planned (dry run)'}")
    return 0


def _cmd_create_goal(args, log) -> int:
    """Run the `create-goal` subcommand; returns the process exit code."""
    from .runner.quest_client import QuestClient, QuestApiError, QuestNotConfigured
    base_url = os.getenv("QUEST_BASE_URL", "")
    api_key = os.getenv("QUEST_API_KEY", "")
    team_id = os.getenv("QUEST_TEAM_ID", "")
    if not base_url or not api_key:
        log.error("QUEST_BASE_URL and QUEST_API_KEY must be set")
        return 1
    client = QuestClient(base_url, api_key, team_id=team_id)
    period = args.period or date.today().isoformat()
    try:
        goal = client.create_goal(
            args.title,
            period=period,
            quest_id=args.quest_id,
            description=args.description,
            criteria=args.criteria,
            goal_type=args.goal_type,
            parent_goal_id=args.parent_goal_id,
            outcome_id=getattr(args, "outcome_id", None),
            ai_help=True if args.ai_help else None,
            assigned_to_user_id=args.assigned_to_user_id,
        )
    except (QuestApiError, QuestNotConfigured) as e:
        log.error("failed to create goal: %s", e)
        print(f"Could not create the goal: {e}")
        return 1
    goal_id = goal.get("id")
    if not goal_id:
        # Never ack a goal that may not exist -- same contract as `send`.
        log.error("goal creation returned no id: %s", goal)
        print("Could not create the goal: the Quest API returned no goal id.")
        return 1
    where = f"on quest {args.quest_id}" if args.quest_id else "standalone (no quest)"
    print(f"Goal created {where}: {args.title}  "
          f"(period {period}, deadline {goal.get('deadline')}, id {goal_id})")
    return 0


def _cmd_sync_quest_folder(args, log) -> int:
    """Run the `sync-quest-folder` subcommand; returns the process exit code."""
    from .runner.quest_client import QuestClient
    from .runner.quest_folder_sync import QuestFolderSyncError, sync_quest_folder
    base_url = os.getenv("QUEST_BASE_URL", "")
    api_key = os.getenv("QUEST_API_KEY", "")
    team_id = os.getenv("QUEST_TEAM_ID", "")
    if not base_url or not api_key:
        log.error("QUEST_BASE_URL and QUEST_API_KEY must be set")
        return 1
    client = QuestClient(base_url, api_key, team_id=team_id)
    try:
        result = sync_quest_folder(client, args.quest_id, args.folder, direction=args.direction,
                                   spec=not getattr(args, "no_spec", False))
    except QuestFolderSyncError as e:
        log.error("sync failed: %s", e)
        return 1
    parts = []
    if result.pulled:
        parts.append(f"pulled {result.notes_pulled} note(s)")
        if result.spec_records is not None:
            parts.append(f"{result.spec_records} spec record(s)")
    if result.pushed:
        parts.append(f"pushed {result.notes_pushed} note(s)")
    print(f"Synced quest {args.quest_id} -> {result.sync_path} ({', '.join(parts)})")
    return 0
