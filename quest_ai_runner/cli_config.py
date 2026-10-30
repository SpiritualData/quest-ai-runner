"""CLI configuration: building a RunnerConfig from environment variables (model provider, channel transport, ask classifier, goal judge).

Split out of ``cli.py`` by definition; that module re-exports every name here.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from .adapters import (
    ClaudeConversationsAdapter,
    CompositeRetrievalAdapter,
    FilesAdapter,
)
from hubofhubs.ports.model import ModelProvider
from .core.prompt_specs import PromptSpec, register, render_prompt
from .core.resolve import resolve
from .adapters.openclaw_channel import OpenClawChannel, OpenClawChannelConfig
from .config import (
    RunnerConfig,
    apply_config_environment,
    apply_file_defaults,
    parse_decision_assignees,
    resolve_config_objects,
)


def _model_provider_from_env() -> ModelProvider:
    """Pick the model backend from env.

    ``QAR_MODEL_BACKEND`` specifies: "anthropic", "claude_cli", "gemini", or "openai".
    Auto-select if not set: tries "openai" (OPENAI_API_KEY) → "gemini" (GOOGLE_API_KEY) →
    "anthropic" (ANTHROPIC_API_KEY) → "claude_cli" (keyless, subscription login).
    """
    backend = (os.getenv("QAR_MODEL_BACKEND") or "").strip().lower()
    if not backend:
        # Auto-select: openai > gemini > anthropic > claude_cli (keyless default)
        if os.getenv("OPENAI_API_KEY"):
            backend = "openai"
        elif os.getenv("GOOGLE_API_KEY"):
            backend = "gemini"
        elif os.getenv("ANTHROPIC_API_KEY"):
            backend = "anthropic"
        else:
            backend = "claude_cli"

    if backend == "multi_provider":
        raise ValueError("QAR_MODEL_BACKEND names the primary vendor; multi_provider is the router build_orchestrator "
                         "wraps around it, so choose anthropic, claude_cli, gemini, openai or deepseek.")
    try:
        provider_cls = resolve("call-a-model", backend)   # the declared implementations, core/resolve.py
    except ValueError as e:
        raise ValueError(f"Unknown QAR_MODEL_BACKEND: {backend}. {e}") from e
    kwargs = {}
    if backend == "claude_cli":
        kwargs["claude_path"] = os.getenv("QAR_CLAUDE_PATH", "claude")
        # Headless completions over a large corpus can take a while; let the consumer raise the
        # per-call wall-clock cap above the conservative default rather than failing the run.
        if os.getenv("QAR_ANSWER_TIMEOUT"):
            kwargs["timeout_seconds"] = float(os.environ["QAR_ANSWER_TIMEOUT"])
        # Cap extended thinking on routing decisions only (see ClaudeCliProvider and
        # docs/cheap-model-routing.md): "0" turns it off, which measured 11x faster on haiku at the
        # same accuracy. Unset leaves the CLI default.
        if (os.getenv("QAR_CLI_PLAN_THINKING_TOKENS") or "").strip().isdigit():
            kwargs["plan_thinking_tokens"] = int(os.environ["QAR_CLI_PLAN_THINKING_TOKENS"].strip())
    return provider_cls(**kwargs)


def _channel_transport_from_env():
    """Build the `channel` subcommand's ``ChannelTransport`` from env, or None.

    Only "openclaw" is a built-in provider today (the reference bridge). A consumer that wants a
    different ``ChannelTransport`` builds ``RunnerConfig`` itself and sets ``channel_transport``
    directly instead of going through this env-driven CLI path — see ``core.adapters_channels.ChannelTransport``.
    """
    provider = (os.getenv("QAR_CHANNEL_PROVIDER") or "").strip().lower()
    if not provider:
        return None
    if provider != "openclaw":
        raise ValueError(f"Unknown QAR_CHANNEL_PROVIDER: {provider!r}. Only 'openclaw' is built in.")
    token_file = (os.getenv("QAR_OPENCLAW_TOKEN_FILE") or "").strip()
    args_env = (os.getenv("QAR_OPENCLAW_ARGS") or "").strip()
    cfg = OpenClawChannelConfig(
        token_file=token_file,
        command=os.getenv("QAR_OPENCLAW_COMMAND", "openclaw"),
        args=(["mcp", "serve"] + args_env.split()) if args_env else ["mcp", "serve"],
        cwd=os.getenv("QAR_OPENCLAW_CWD") or None,
        call_timeout_s=float(os.getenv("QAR_OPENCLAW_CALL_TIMEOUT", "30")),
        channel=os.getenv("QAR_CHANNEL_NAME", "openclaw"),
    )
    return OpenClawChannel(cfg)


def _apply_channel_env(cfg: RunnerConfig) -> None:
    """Fold the channel-specific env vars (see the module docstring) into an existing RunnerConfig,
    in place. Separate from ``_config_from_env`` so the ordinary `poll`/`chat` paths never pay for
    (or accidentally pick up) channel config they didn't ask for."""
    cfg.channel_transport = _channel_transport_from_env()
    senders = (os.getenv("QAR_CHANNEL_ALLOWED_SENDERS") or "").strip()
    if senders:
        cfg.channel_allowed_senders = [s.strip() for s in senders.split(",") if s.strip()]
    if os.getenv("QAR_CHANNEL_ACK_AFTER_SECONDS"):
        cfg.channel_ack_after_seconds = float(os.environ["QAR_CHANNEL_ACK_AFTER_SECONDS"])
    if os.getenv("QAR_CHANNEL_PROGRESS_MIN_SECONDS"):
        cfg.channel_progress_min_seconds = float(os.environ["QAR_CHANNEL_PROGRESS_MIN_SECONDS"])
    if os.getenv("QAR_CHANNEL_TURN_TIMEOUT_SECONDS"):
        cfg.channel_turn_timeout_seconds = float(os.environ["QAR_CHANNEL_TURN_TIMEOUT_SECONDS"])
    if os.getenv("QAR_CHANNEL_STATE_PATH"):
        cfg.channel_state_path = os.environ["QAR_CHANNEL_STATE_PATH"]


ASK_CLASSIFY_SYSTEM = (
    "You sort one open note or request from a team's project quest. Reply with one JSON object only: "
    '{"kind": "request" | "standing" | "context" | "question" | "unknown", "assignee_id": string or null, '
    '"confidence": number from 0 to 1, "reason": one short sentence, "title": a short action title of at most "'
    "80 characters, for example 'Confirm the Figma access for the mentor screens'}. "
    "Use kind request only when someone must do one specific thing that is finished once; set assignee_id "
    "to the single member of the roster whose listed goals show that work is theirs. Use null when the work "
    "belongs to no single member, and use unknown when you cannot tell. A note that only reports, thanks, or "
    "gives feedback is context. Never invent a member id. Do not use em dashes."
)


ASSIGNED_GOAL_JUDGE_SYSTEM = (
    "You judge one goal from a team's project quest. Reply with one JSON object only: "
    '{"ai_doable": true or false, "confidence": number from 0 to 1, "reason": one short sentence}. '
    "ai_doable is true only when an AI assistant could finish the work itself from text and the tools it has, "
    "such as drafting, reading figures from the quest, summarising, or writing a plan. It is false when the work "
    "needs a person's voice, a meeting, a physical action, a decision only a person can make, or contact with "
    "someone outside the team. Never invent facts. Do not use em dashes."
)


GOAL_JUDGE_SPEC = register(PromptSpec(
    id="assist-a-terminal-session.goal-judge", solution="assist-a-terminal-session",
    metric="metric-assist-a-terminal-session", tier="balanced", cache_layer="head",
    template=ASSIGNED_GOAL_JUDGE_SYSTEM,
    output_schema={"ai_doable": "boolean", "confidence": "number 0 to 1", "reason": "one sentence"},
))

ASK_CLASSIFY_SPEC = register(PromptSpec(
    id="assist-a-terminal-session.ask-classify", solution="assist-a-terminal-session",
    metric="metric-assist-a-terminal-session", tier="balanced", cache_layer="head",
    template=ASK_CLASSIFY_SYSTEM,
))


def _quest_model():
    """(provider, model) for the balanced tier, or (None, None) when no model is configured. Never raises."""
    try:
        from .config import build_orchestrator
        from .core.model_registry import ModelRegistry
        cfg = _config_from_env()
        build_orchestrator(cfg)
        provider = cfg.model_provider
        if provider is None:
            return None, None
        return provider, ModelRegistry(provider, fallback=cfg.model_fallback or None).resolve_tier("balanced")
    except Exception as e:  # noqa: BLE001 -- reconcile must still run without the model
        log.warning("model unavailable for reconcile judgements: %s", e)
        return None, None


def _goal_judge(quest_id: str, client):
    """The model that judges whether an assigned goal is AI work, or None without a model."""
    from .core.card_filter import _extract_json
    provider, model = _quest_model()
    if provider is None:
        return None

    def judge(goal):
        prompt = json.dumps({"goal": goal.get("name", ""), "description": (goal.get("description") or "")[:800],
                             "criteria": (goal.get("criteria") or "")[:400]}, ensure_ascii=False)
        try:
            raw = provider.answer([{"role": "user", "content": prompt}], model=model,
                                  system=render_prompt("assist-a-terminal-session.goal-judge"))
            return json.loads(_extract_json(raw))
        except Exception as e:  # noqa: BLE001 -- one bad answer leaves that goal with its owner
            log.warning("goal %s not judged: %s", goal.get("id"), e)
            return None

    return judge


def _ask_classifier(quest_id: str, client):
    """The model that sorts open asks for ``reconcile-plan``, or None when no model is configured.

    Returns None rather than failing, so reconcile still runs its plain planning without a model. The model
    sees only the note, the quest's members, and each member's current goal names as evidence of their work.
    """
    from .core.card_filter import _extract_json
    provider, model = _quest_model()
    if provider is None:
        log.warning("ask classification unavailable, asks stay unclassified")
        return None

    goal_names: Dict[str, List[str]] = {}
    try:
        for pg in client.list_quest_goals(quest_id).get("period_groups", []):
            for g in pg.get("goals", []):
                if g.get("assigned_to_user_id") and not g.get("completed"):
                    names = goal_names.setdefault(g["assigned_to_user_id"], [])
                    if len(names) < 8:
                        names.append(g.get("name", ""))
    except Exception as e:  # noqa: BLE001 -- evidence is optional
        log.warning("could not read member goals for ask classification: %s", e)

    def classify(ask, roster):
        members = [{"user_id": m.get("user_id"), "name": m.get("name"),
                    "current_goals": goal_names.get(str(m.get("user_id")), [])} for m in roster]
        prompt = json.dumps({"note": ask.get("text", ""), "author": ask.get("author", ""),
                             "members": members}, ensure_ascii=False)
        try:
            raw = provider.answer([{"role": "user", "content": prompt}], model=model,
                                  system=render_prompt("assist-a-terminal-session.ask-classify"))
            return json.loads(_extract_json(raw))
        except Exception as e:  # noqa: BLE001 -- one bad answer leaves that ask unclassified
            log.warning("ask %s not classified: %s", ask_key_of(ask), e)
            return None

    return classify


def ask_key_of(ask) -> str:
    return str(ask.get("key") or ask.get("id") or "")


def float_or_none(raw: Optional[str]) -> Optional[float]:
    """Parse an env var as a float, or None if unset/blank/unparseable.

    Unparseable is logged and treated as unset rather than raising -- a typo in an optional
    numeric env var should degrade the ONE feature it configures, not crash startup.
    """
    text = (raw or "").strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        logging.getLogger("quest-ai-runner").warning(
            "float_or_none: expected a number, got %r; ignoring", text)
        return None


def _team_ids_from_env(raw: Optional[str]) -> List[str]:
    """Parse ``QUEST_TEAM_IDS`` ("a, b ,,a") into an ordered, de-duplicated list (``["a", "b"]``).

    Unset, empty, or nothing but separators gives ``[]`` -- the dataclass default, which is what
    keeps single-team mode byte-identical and lets a config file's own ``team_ids`` still win
    (``apply_file_defaults`` only fills fields still sitting at their default).

    Order is preserved on first sight rather than sorted: the list becomes a comma-joined query
    param, and an operator reading a request log should see the teams in the order they wrote
    them. Duplicates are dropped because a repeated id would only widen the param string, never
    the result.
    """
    ids: List[str] = []
    for part in (raw or "").split(","):
        team = part.strip()
        if team and team not in ids:
            ids.append(team)
    return ids


def _config_from_env(config_path: Optional[str] = None) -> RunnerConfig:
    """Build a ``RunnerConfig`` from the environment (see the module docstring for the full list),
    optionally layered on top of a TOML config file.

    ``config_path`` (or ``QAR_CONFIG_FILE`` when the argument is omitted) names a file read via
    ``RunnerConfig.from_file`` — see ``docs/writing-a-consumer.md``. Precedence: the FILE supplies
    a baseline for whichever fields it sets; an environment variable that also sets a field always
    wins (``config.apply_file_defaults``, applied once at the end, only fills fields no env var
    touched). A bad file (missing, invalid TOML, an unknown or non-file-expressible key) raises
    ``config.ConfigFileError`` — loudly, at startup, same as every other config problem this
    function surfaces via ``RunnerConfig.validate()``.
    """
    config_path = config_path or os.getenv("QAR_CONFIG_FILE") or None
    file_cfg = RunnerConfig.from_file(config_path) if config_path else None
    # BEFORE any env-driven resolution below: the file's own env_files/env_aliases/env populate
    # os.environ, so a lane can express its credential files, its variable renames, and every
    # QAR_* knob (including the nested OrchestratorConfig ones no TOML field reaches) in the same
    # file as everything else, instead of in a Python consumer written once per lane.
    apply_config_environment(file_cfg)

    corpus = os.getenv("QAR_CORPUS_ROOT") or (file_cfg.corpus_root if file_cfg else None)
    retrieval = FilesAdapter(corpus) if corpus else None

    # Live web search is NOT folded in here: broadcasting it through CompositeRetrievalAdapter
    # would fire a paid, slow web search on every ordinary corpus grep/query. ``build_orchestrator``
    # wires ``cfg.web_research`` separately (see ``adapters/web_research.build_web_research_from_env``),
    # reached only by the planner's own {"web": ...}/{"web_page": ...} read keys.

    # Add conversation search unless explicitly disabled. Two separate adapters:
    # 1. Claude Code sessions (~/.claude/sessions) — read-only; written by Claude Code, not QAR.
    # 2. QAR's own chat history (~/.quest-ai-runner/conversations) — read+write by QAR.
    _conv_search = (os.getenv("QAR_CONVERSATION_SEARCH") or "").strip().lower()
    if _conv_search not in ("false", "0", "off", "no"):
        conv_adapters = []
        # Claude Code sessions (read-only for QAR)
        sessions_dir = os.getenv("QAR_CLAUDE_SESSIONS_DIR") or None
        try:
            conv_adapters.append(ClaudeConversationsAdapter(
                corpus_root=corpus,
                sessions_dir=sessions_dir,
            ))
        except Exception:
            pass
        # QAR's own chat history (read+write by QAR)
        chat_history_dir = os.getenv("QAR_CHAT_HISTORY_DIR") or str(
            Path.home() / ".quest-ai-runner" / "conversations"
        )
        if chat_history_dir:
            try:
                conv_adapters.append(ClaudeConversationsAdapter(sessions_dir=chat_history_dir))
            except Exception:
                pass
        for adapter in conv_adapters:
            if retrieval is not None:
                retrieval = CompositeRetrievalAdapter([retrieval, adapter])
            else:
                retrieval = adapter

    # The deep runner is NOT wired here any more: leaving RunnerConfig.deep_runner unset means
    # "auto", and config.resolve_deep_runner builds the same SubprocessGoalRunner from the same
    # env vars (QAR_DEEP_WORKING_DIR, falling back to corpus_root and then cwd; QAR_CLAUDE_PATH).
    # Building it here as well would only fork the logic — and the old `deep_runner = None` for a
    # corpus-less run read as "execution deliberately disabled", so `qar chat` with no corpus
    # configured could never execute anything.
    # Allow model tier overrides via env vars: QAR_MODEL_FAST, QAR_MODEL_BALANCED, QAR_MODEL_QUALITY, QAR_MODEL_BEST
    model_fallback = {}
    for tier in ("fast", "balanced", "quality", "best"):
        env_key = f"QAR_MODEL_{tier.upper()}"
        if os.getenv(env_key):
            model_fallback[tier] = os.getenv(env_key)

    cfg = RunnerConfig(
        quest_base_url=os.getenv("QUEST_BASE_URL", ""),
        quest_api_key=os.getenv("QUEST_API_KEY", ""),
        team_id=os.getenv("QUEST_TEAM_ID", ""),
        org_id=os.getenv("QUEST_ORG_ID", ""),
        # None (unset) falls back to team_id for discovery; "" (explicitly set empty) means
        # owner-scoped discovery instead — os.environ.get (no default) is what preserves that
        # three-way distinction. A personal/single-user lane whose tasks are created owner-scoped
        # (null team_id) needs QAR_DISCOVERY_TEAM_ID="" for exactly this reason.
        discovery_team_id=os.environ.get("QAR_DISCOVERY_TEAM_ID"),
        # The SET of teams ONE lane discovers from. Unset/empty leaves the dataclass default
        # ([]), which is single-team mode exactly as before -- and leaving it AT the default is
        # also what lets a config file's own ``team_ids`` win here (apply_file_defaults only
        # fills fields no env var moved off their default).
        team_ids=_team_ids_from_env(os.getenv("QUEST_TEAM_IDS")),
        # Claim allowlist for an owner-scoped lane (same comma-separated shape as QUEST_TEAM_IDS);
        # unset leaves the default [] (no restriction) so a config file's own ``claim_teams`` wins.
        claim_teams=_team_ids_from_env(os.getenv("QUEST_CLAIM_TEAMS")),
        retrieval=retrieval,
        model_provider=_model_provider_from_env(),
        model_fallback=model_fallback or None,
        corpus_root=corpus,
        runner_label=os.getenv("QAR_RUNNER_LABEL") or None,
        env_id=os.getenv("QAR_ENV_ID") or None,
        lane_user_id=os.getenv("QAR_LANE_USER_ID") or None,
        # Who human-only confirm/decision requests route to by default (QuestDecisionSink's
        # fallback assignee). Documented since custom_consumer.py/.env.example but never actually
        # wired into the stock CLI's env reading until now.
        default_assignee_user_id=os.getenv("QAR_DECISION_ASSIGNEE") or None,
        # Named routing roles, e.g. QAR_DECISION_ASSIGNEES='{"owner":"user_a","operator":"user_b"}'
        # — what lets a caller say create_decision(..., assignee="operator") instead of carrying
        # the deployment's routing policy into every call site.
        decision_assignees=parse_decision_assignees(os.getenv("QAR_DECISION_ASSIGNEES")),
        decision_default_deadline_hours=float_or_none(
            os.getenv("QAR_DECISION_DEFAULT_DEADLINE_HOURS")),
    )
    if os.getenv("QAR_MAX_PARALLEL"):
        cfg.orchestrator.max_parallel = int(os.environ["QAR_MAX_PARALLEL"])
    if os.getenv("QAR_POLL_INTERVAL"):
        cfg.poll_interval_seconds = float(os.environ["QAR_POLL_INTERVAL"])
    # Routing cost and small-model reliability (see docs/cheap-model-routing.md). Both live on
    # OrchestratorConfig, like QAR_MAX_PARALLEL above. The reach judge is left inert unless the
    # consumer also supplies ``read_reach_summary``: the library cannot write that statement for a
    # deployment it knows nothing about, and a judge given a generic guess is worse than none.
    if os.getenv("QAR_TASK_DIRECT_DEEP"):
        cfg.orchestrator.task_direct_deep = (
            os.environ["QAR_TASK_DIRECT_DEEP"].strip().lower() in ("1", "true", "on", "yes"))
    if os.getenv("QAR_PLANNER_PROMPT_PROFILE"):
        cfg.orchestrator.planner_prompt_profile = (
            os.environ["QAR_PLANNER_PROMPT_PROFILE"].strip().lower())
    if os.getenv("QAR_PLANNER_REACH_JUDGE"):
        cfg.orchestrator.planner_reach_judge = (
            os.environ["QAR_PLANNER_REACH_JUDGE"].strip().lower()
            in ("1", "true", "on", "yes"))
    if os.getenv("QAR_PLANNER_REACH_JUDGE_TIER"):
        cfg.orchestrator.planner_reach_judge_tier = (
            os.environ["QAR_PLANNER_REACH_JUDGE_TIER"].strip().lower())
    # What THIS lane's reads reach, for the reach judge: inline, or a file (the usual form, since
    # it is a paragraph about the lane's own corpus and the environments it hands work to). A file
    # that cannot be read is logged and ignored, which leaves the judge inert, never guessing.
    reach_summary = (os.getenv("QAR_READ_REACH_SUMMARY") or "").strip()
    reach_file = (os.getenv("QAR_READ_REACH_SUMMARY_FILE") or "").strip()
    if reach_file:
        try:
            from_file = Path(reach_file).expanduser().read_text(encoding="utf-8").strip()
        except OSError as e:
            from_file = ""
            logging.getLogger("quest-ai-runner.cli").warning(
                "QAR_READ_REACH_SUMMARY_FILE could not be read (%s)", e)
        if from_file:
            reach_summary = from_file
        else:
            logging.getLogger("quest-ai-runner.cli").warning(
                "QAR_READ_REACH_SUMMARY_FILE gave no summary; %s", "using QAR_READ_REACH_SUMMARY"
                if reach_summary else "the reach judge stays inert")
    if reach_summary:
        cfg.orchestrator.read_reach_summary = reach_summary
    # QAR_DEEP_MAX_TURNS: hard per-attempt turn cap for the deep goal loop (lives on
    # OrchestratorConfig, not RunnerConfig directly, same as QAR_GOAL_TOKEN_BUDGET/
    # QAR_GOAL_MAX_ATTEMPTS below). The library default (30) is tight for a lane whose tasks
    # routinely chain several real actions in one run.
    if os.getenv("QAR_DEEP_MAX_TURNS"):
        try:
            cfg.orchestrator.deep_max_turns = int(os.environ["QAR_DEEP_MAX_TURNS"])
        except ValueError:
            pass
    # --- rep-sync direction (opt-in; only takes effect once a rep_sync_resolver/personas policy
    # is also wired — see RunnerConfig.rep_sync_resolver / RunnerConfig.personas) --------------
    if os.getenv("QAR_REP_SYNC_DIRECTION"):
        cfg.rep_sync_direction = os.environ["QAR_REP_SYNC_DIRECTION"].strip().lower()
    # --- autopilot tuning (opt-in per quest; see RunnerConfig.autopilot_*) ----------------------
    if os.getenv("QAR_AUTOPILOT_PASS_TIME"):
        cfg.autopilot_pass_time = os.environ["QAR_AUTOPILOT_PASS_TIME"].strip()
    _adopt_recurring = (os.getenv("QAR_AUTOPILOT_ADOPT_RECURRING") or "").strip().lower()
    if _adopt_recurring in ("1", "true", "on", "yes"):
        cfg.autopilot_adopt_recurring = True
    elif _adopt_recurring in ("0", "false", "off", "no"):
        cfg.autopilot_adopt_recurring = False
    # --- deep-run context preamble (org/persona doctrine prepended to every deep brief) ---------
    # QAR_CONTEXT_PREAMBLE_FILE (a file path) is preferred: multi-line org doctrine is miserable as
    # a bare env var. QAR_CONTEXT_PREAMBLE (inline) still works for a short one-liner and is kept
    # for consumers already using it (see examples/custom_consumer.py, .env.example); the file
    # variant wins when both are set. Only reaches the auto-built deep runner (see
    # config.resolve_deep_runner) -- an explicit RunnerConfig.deep_runner instance is unaffected.
    _preamble_file = os.getenv("QAR_CONTEXT_PREAMBLE_FILE")
    if _preamble_file:
        try:
            cfg.context_preamble = Path(_preamble_file).read_text(encoding="utf-8")
        except OSError as e:
            logging.getLogger("quest-ai-runner").warning(
                "QAR_CONTEXT_PREAMBLE_FILE=%r could not be read (%s); no context preamble set",
                _preamble_file, e)
    elif os.getenv("QAR_CONTEXT_PREAMBLE"):
        cfg.context_preamble = os.environ["QAR_CONTEXT_PREAMBLE"]

    # --- quest <-> local folder sync (opt-in; see RunnerConfig.quest_folder_map) ---------------
    # QAR_QUEST_FOLDER_MAP: a JSON object mapping quest/goal id -> local folder, e.g.
    #   QAR_QUEST_FOLDER_MAP={"quest_1625d9f47a06": "/srv/corpus/concept_ai_interface_quest"}
    # Every entry is kept in sync with its quest EVERY scan (Poller.run_once, so the standing
    # poll/systemd process keeps it current with no task required) plus, opt-in, right before/after
    # any task whose goal_id/quest_id matches an entry.
    if os.getenv("QAR_QUEST_FOLDER_MAP"):
        try:
            parsed = json.loads(os.environ["QAR_QUEST_FOLDER_MAP"])
            if isinstance(parsed, dict):
                cfg.quest_folder_map = {str(k): str(v) for k, v in parsed.items()}
            else:
                logging.getLogger("quest-ai-runner").warning(
                    "QAR_QUEST_FOLDER_MAP must be a JSON object; ignoring")
        except (ValueError, TypeError) as e:
            logging.getLogger("quest-ai-runner").warning(
                "QAR_QUEST_FOLDER_MAP is not valid JSON (%s); ignoring", e)
    if os.getenv("QAR_QUEST_FOLDER_SYNC_DIRECTION"):
        cfg.quest_folder_sync_direction = os.environ["QAR_QUEST_FOLDER_SYNC_DIRECTION"].strip().lower()

    # --- the feedback ledger (on by default; see runner/feedback_ledger.py) ---------------------
    if os.getenv("QAR_FEEDBACK_LEDGER"):
        cfg.feedback_ledger = os.environ["QAR_FEEDBACK_LEDGER"].strip().lower() not in (
            "0", "false", "no", "off")
    if os.getenv("QAR_FEEDBACK_LEDGER_PATH"):
        cfg.feedback_ledger_path = os.environ["QAR_FEEDBACK_LEDGER_PATH"]

    # --- automated context updates (on by default; see runner/context_updates.py) ---------------
    if os.getenv("QAR_CONTEXT_UPDATES"):
        cfg.context_updates = os.environ["QAR_CONTEXT_UPDATES"].strip().lower() not in (
            "0", "false", "no", "off")
    if os.getenv("QAR_CONTEXT_UPDATES_STATE_PATH"):
        cfg.context_updates_state_path = os.environ["QAR_CONTEXT_UPDATES_STATE_PATH"]
    if os.getenv("QAR_CONTEXT_UPDATES_FIRST_LOOK_DAYS"):
        try:
            cfg.context_updates_first_look_days = int(
                os.environ["QAR_CONTEXT_UPDATES_FIRST_LOOK_DAYS"])
        except ValueError:
            logging.getLogger("quest-ai-runner").warning(
                "QAR_CONTEXT_UPDATES_FIRST_LOOK_DAYS is not an integer; using the default")

    # --- fast lane for real-time tasks (D2 revised: presence-aware push) -------------------------
    # QAR_WAIT_CHANNEL: "0"/"false"/"off" disables the long-poll wait channel, falling back to a
    # short interval poll (QAR_CONTEXT_POLL_SECONDS). Default: enabled.
    _wait_channel = (os.getenv("QAR_WAIT_CHANNEL") or "").strip().lower()
    if _wait_channel in ("0", "false", "off", "no"):
        cfg.wait_channel_enabled = False
    elif _wait_channel in ("1", "true", "on", "yes"):
        cfg.wait_channel_enabled = True
    if os.getenv("QAR_CONTEXT_POLL_SECONDS"):
        try:
            cfg.context_poll_seconds = float(os.environ["QAR_CONTEXT_POLL_SECONDS"])
        except ValueError:
            pass
    if os.getenv("QAR_WAIT_TIMEOUT_SECONDS"):
        try:
            cfg.wait_timeout_seconds = float(os.environ["QAR_WAIT_TIMEOUT_SECONDS"])
        except ValueError:
            pass
    # The planner step picks the next action (read/answer/deep/confirm). It defaults to a cheap
    # tier; a consumer whose tasks are mostly real work (e.g. code edits) can raise it so the
    # answer-vs-deep routing is decided by a more capable model.
    planner_tier = (os.getenv("QAR_PLANNER_TIER") or "").strip().lower()
    if planner_tier:
        cfg.orchestrator.planner_tier = planner_tier
    # QAR_PLANNER_MODEL pins the ROUTING DECISION alone to one model id, leaving everything else
    # on QAR_PLANNER_TIER's model (docs/cheap-model-routing.md).
    planner_model = (os.getenv("QAR_PLANNER_MODEL") or "").strip()
    if planner_model:
        cfg.orchestrator.planner_model = planner_model

    # --- Deep goal loop tuning (our own /goal replacement) -------------------------------------
    # The deep worker is Claude Code, so it can ONLY run Claude models. QAR_DEEP_MODELS is the
    # model LADDER the goal loop escalates through on a not-met goal, weak -> strong, e.g.
    # "claude-sonnet-4-6,claude-opus-4-8" or the bare aliases "sonnet,opus". Give REAL Claude
    # model ids/aliases here, not semantic tier names ("fast"/"balanced"/"quality"/"best") -- those
    # are tier names the ModelRegistry resolves per-DEPLOYMENT, which on a Gemini/OpenAI deployment
    # resolve to non-Claude ids the worker cannot run, silently making the ladder inert (see
    # HANDS_FREE_QUEST_AI_DESIGN.md section 2, point 4). Left UNSET (the default): the ladder is
    # NOT hardcoded here -- Orchestrator._deep_models resolves one generically at run time (pins to
    # an already-Claude-runnable fallback model, then tries to extend it with any Claude id found by
    # resolving the "quality"/"best" tiers, e.g. from QAR_MODEL_BEST), and logs what it found.
    deep_models_env = (os.getenv("QAR_DEEP_MODELS") or "").strip()
    if deep_models_env:
        cfg.orchestrator.deep_model_ladder = (
            [m.strip() for m in deep_models_env.split(",") if m.strip()] or None
        )
    # Automatic STARTING model for deep runs nobody pinned (core/deep_model_selection.py): the
    # planner rates the work simple/normal/hard on the call it already makes and the run starts on
    # that difficulty's model, escalating up the ladder above (or haiku,sonnet,opus when
    # QAR_DEEP_MODELS is unset) on a not-met goal. QAR_DEEP_AUTO_MODEL=0/false/off disables it (the
    # ladder then starts at its first rung, as before). QAR_DEEP_MODEL_SIMPLE / _NORMAL / _HARD set
    # the per-difficulty start (defaults haiku / sonnet / sonnet). Like QAR_DEEP_MODELS these must be
    # Claude ids/aliases the deep worker can run, never tier names; an unusable value is ignored.
    _auto = (os.getenv("QAR_DEEP_AUTO_MODEL") or "").strip().lower()
    if _auto in ("0", "false", "off", "no"):
        cfg.orchestrator.deep_auto_model = False
    elif _auto in ("1", "true", "on", "yes"):
        cfg.orchestrator.deep_auto_model = True
    from .core.goal_runner import cli_safe_model
    for level, var in (("simple", "QAR_DEEP_MODEL_SIMPLE"), ("normal", "QAR_DEEP_MODEL_NORMAL"),
                       ("hard", "QAR_DEEP_MODEL_HARD")):
        value = (os.getenv(var) or "").strip()
        if not value:
            continue
        if not cli_safe_model(value):   # also rejects tier names such as "fast" or "best"
            logging.getLogger("quest-ai-runner").warning(
                "%s=%r is not a Claude model the deep worker can run; ignoring it", var, value)
            continue
        cfg.orchestrator.deep_difficulty_models[level] = value
    # Overall TOKEN BUDGET for one turn's deep goal loop (worker tokens summed across attempts).
    # Operator-tunable; replaces a fixed attempt count as the primary stop.
    if os.getenv("QAR_GOAL_TOKEN_BUDGET"):
        try:
            cfg.orchestrator.deep_goal_token_budget = int(os.environ["QAR_GOAL_TOKEN_BUDGET"])
        except ValueError:
            pass
    # Hard safety cap on attempts (the budget is the primary control; this just bounds runaway).
    if os.getenv("QAR_GOAL_MAX_ATTEMPTS"):
        try:
            cfg.orchestrator.deep_goal_max_iterations = int(os.environ["QAR_GOAL_MAX_ATTEMPTS"])
        except ValueError:
            pass
    # --- Async post-deep context-card updater (prepare for the future) -------------------------
    # ON by default; set QAR_ASYNC_CARD_UPDATE=0/false/off to disable (then deep is unchanged and no
    # future-context section is appended to deep briefs). Inert anyway without a card-update store.
    _acu = (os.getenv("QAR_ASYNC_CARD_UPDATE") or "").strip().lower()
    if _acu in ("0", "false", "off", "no"):
        cfg.orchestrator.async_card_update = False
    elif _acu in ("1", "true", "on", "yes"):
        cfg.orchestrator.async_card_update = True
    # --- Minimal-intervention overseer (the simulated-conscious judge) -------------------------
    # QAR_OVERSEER=0/false disables it (library default: ON). A high-quality model reads a tiny
    # capped digest of the run and almost always stays silent; occasionally it sends ONE signal
    # (redirect / answer_now / escalate_deep / escalate_human). Consults are non-blocking (a
    # background thread polled without waiting), pre-filtered by a free heuristic gate, and capped
    # at QAR_OVERSEER_MAX_SIGNALS per run, so the risk coverage costs at most a few small calls.
    _ovr = (os.getenv("QAR_OVERSEER") or "").strip().lower()
    if _ovr in ("1", "true", "on", "yes"):
        cfg.orchestrator.overseer = True
    elif _ovr in ("0", "false", "off", "no"):
        cfg.orchestrator.overseer = False
    # QAR_OVERSEER_TIER: the judge's model tier (default "quality": judgment is where the stronger
    # model pays; the digest keeps its token cost tiny regardless).
    if (os.getenv("QAR_OVERSEER_TIER") or "").strip():
        cfg.orchestrator.overseer_tier = os.environ["QAR_OVERSEER_TIER"].strip().lower()
    if os.getenv("QAR_OVERSEER_MAX_SIGNALS"):
        try:
            cfg.orchestrator.overseer_max_signals = int(os.environ["QAR_OVERSEER_MAX_SIGNALS"])
        except ValueError:
            pass
    # QAR_VERIFY_TIER: the goal/claims verification judge's tier (default "balanced": this one small
    # hard-capped call gates done vs needs_you/failed and claim honesty). Set "quality" to strengthen.
    if (os.getenv("QAR_VERIFY_TIER") or "").strip():
        cfg.orchestrator.verify_tier = os.environ["QAR_VERIFY_TIER"].strip().lower()
    # --- Warm recent-turn context fallback (see core/recent_context.py) ------------------------
    # ON by default (library default: True). QAR_RECENT_CONTEXT=0/false disables it entirely (no
    # store is wired by build_orchestrator's resolve_recent_context_store, so recent-turn cards are
    # never consulted or recorded). QAR_RECENT_CONTEXT_MAX_CARDS caps how many recent-turn cards
    # filter_relevant may let through in one turn (default 6; these are ADDITIONAL to whatever the
    # fresh assembly finds).
    _rc = (os.getenv("QAR_RECENT_CONTEXT") or "").strip().lower()
    if _rc in ("0", "false", "off", "no"):
        cfg.orchestrator.recent_context_enabled = False
    elif _rc in ("1", "true", "on", "yes"):
        cfg.orchestrator.recent_context_enabled = True
    if os.getenv("QAR_RECENT_CONTEXT_MAX_CARDS"):
        try:
            cfg.orchestrator.recent_context_max_cards = int(os.environ["QAR_RECENT_CONTEXT_MAX_CARDS"])
        except ValueError:
            pass
    # QAR_RECENT_CONTEXT_GLOBAL: the recent-context store's "global" scope (everything recently
    # selected anywhere, not just this conversation/quest) is ON by default. "0"/"false" disables
    # ONLY cross-conversation/cross-quest memory; conv- and quest-scoped warm context (and the
    # QAR_RECENT_CONTEXT master switch above) are unaffected.
    _rcg = (os.getenv("QAR_RECENT_CONTEXT_GLOBAL") or "").strip().lower()
    if _rcg in ("0", "false", "off", "no"):
        cfg.orchestrator.recent_context_global_enabled = False
    elif _rcg in ("1", "true", "on", "yes"):
        cfg.orchestrator.recent_context_global_enabled = True
    # QAR_ANTICIPATION: the anticipation engine (see core/anticipation.py) -- OFF by default.
    # "1"/"true" enables it: build_orchestrator's resolve_anticipator then wires an Anticipator
    # (file store under <cards_dir>/predictions, precompute via the resolved context assembler),
    # so each turn observes/learns ask patterns and pre-plans the next predictions. With the flag
    # off (or unset) no Anticipator is wired and the run is byte-for-byte identical.
    _ant = (os.getenv("QAR_ANTICIPATION") or "").strip().lower()
    if _ant in ("1", "true", "on", "yes"):
        cfg.orchestrator.anticipation_enabled = True
    elif _ant in ("0", "false", "off", "no"):
        cfg.orchestrator.anticipation_enabled = False
    # QAR_ANTICIPATION_LLM: the OPTIONAL one-call-per-turn refresh (see core/anticipation.py
    # Anticipator.refresh) -- OFF by default so the lane stays zero-LLM. "1"/"true" wires a refiner
    # from cfg.model_provider (balanced tier) in resolve_anticipator; it only fires when the main
    # anticipation flag is also on.
    _ant_llm = (os.getenv("QAR_ANTICIPATION_LLM") or "").strip().lower()
    if _ant_llm in ("1", "true", "on", "yes"):
        cfg.orchestrator.anticipation_llm_enabled = True
    elif _ant_llm in ("0", "false", "off", "no"):
        cfg.orchestrator.anticipation_llm_enabled = False
    # QAR_EXPLAIN_ANSWER: the user-facing "Explain how I got this" panel (see
    # core/answer_explanation.py) -- OFF by default. "1"/"true" enables it: an ELIGIBLE turn (one
    # that read, executed, ran deep, answered on assembled context, took more than one step, or
    # searched the web) then emits one extra EVENT_EXPLANATION after its result. Ineligible turns
    # cost nothing and emit nothing. QAR_EXPLAIN_TIER overrides the tier the one extra call
    # resolves to (default "fast": it summarizes a turn that already happened, it does not gate it).
    _explain = (os.getenv("QAR_EXPLAIN_ANSWER") or "").strip().lower()
    if _explain in ("1", "true", "on", "yes"):
        cfg.orchestrator.explain_answer = True
    elif _explain in ("0", "false", "off", "no"):
        cfg.orchestrator.explain_answer = False
    _explain_tier = (os.getenv("QAR_EXPLAIN_TIER") or "").strip().lower()
    if _explain_tier:
        cfg.orchestrator.explain_tier = _explain_tier
    # QAR_LINK_GUARD / QAR_LINK_POLICY_FILE: the link guard (see core/link_guard.py) -- ON by
    # default, because a fabricated URL that reads perfectly is worse than no URL at all. "0"
    # turns it off. QAR_LINK_POLICY_FILE names the JSON that says what "verified" means for this
    # host app (its route table or a routes_file pointing at a generated one, its own origins,
    # trusted hosts, in-app schemes, rewrite rules). With no policy file external URLs are still
    # fetched and checked, while in-app paths have no route table to be checked against, so they
    # come back unverified and are stripped.
    _link_guard = (os.getenv("QAR_LINK_GUARD") or "").strip().lower()
    if _link_guard in ("1", "true", "on", "yes"):
        cfg.orchestrator.link_guard = True
    elif _link_guard in ("0", "false", "off", "no"):
        cfg.orchestrator.link_guard = False
    _link_policy = (os.getenv("QAR_LINK_POLICY_FILE") or "").strip()
    if _link_policy:
        cfg.orchestrator.link_policy_file = _link_policy
    # Config FILE last: fills any field no environment variable above touched, from config_path/
    # QAR_CONFIG_FILE (see RunnerConfig.from_file + config.apply_file_defaults). A field an env var
    # set always wins; this never overrides one.
    apply_file_defaults(cfg, file_cfg)
    # Last: turn the file's declarative pointers (a JSON map, a service-account
    # credential) into the live maps and clients they describe.
    resolve_config_objects(cfg)
    return cfg
