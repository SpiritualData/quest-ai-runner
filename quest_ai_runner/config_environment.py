"""Applying environment and config files: env loading, Quest client from config, decision assignees, Drive MCP.

Split out of ``config.py`` by definition; that module re-exports every name here.
"""
from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING, Any, Dict, Optional
from pathlib import Path

from hubofhubs.act.mcp_client import MCPServerSpec
from .config_support import _AUTO_CONTEXT, _log
from .config_source_builders import (
    _build_drive_comments,
    _build_google_chat,
    _build_inbound_mail,
    _build_notion,
    _load_json_mapping,
)
from .config_errors import ConfigFileError

if TYPE_CHECKING:
    from .config import RunnerConfig


def apply_config_environment(file_cfg: Optional[RunnerConfig]) -> None:
    """Apply a config file's ``env_files`` / ``env_aliases`` / ``env`` to ``os.environ``.

    Called BEFORE anything env-driven is resolved, which is the whole point: every ``QAR_*`` knob
    this library reads then becomes reachable from the config file, including the nested
    ``OrchestratorConfig`` ones (``QAR_MAX_PARALLEL``, ``QAR_DEEP_MAX_TURNS``, ``QAR_DEEP_MODELS``)
    that no TOML field can set, and the multi-line ones best kept in their own file
    (``QAR_CONTEXT_PREAMBLE_FILE``).

    WHY THIS EXISTS. Three lanes on this pattern each kept a Python consumer alive for nothing but
    this: read my ``.env``, rename three of its variables onto the library's names, set two
    ``QAR_*`` defaults, then call ``load_config``. That is not business logic, it is config that
    had no file to live in, and every lane wrote it again slightly differently (one of them
    ``setdefault``, which silently kept a blank placeholder line and dropped the real value).

    Order. A value the PROCESS already held (a systemd unit's own ``Environment=``, an operator's
    shell) wins over all of it; below that:
      1. ``env_aliases`` ({LIBRARY_VAR = "LANE_VAR"}), truthy-checked on both sides, resolved from
         the environment AFTER the files are loaded.
      2. ``env_files``, in order -- earlier files win, later ones are fallbacks.
      3. ``env`` ({VAR = "value"}), plain defaults.

    An alias outranks a file on purpose. Two lanes commonly share a fallback ``.env`` (same person,
    a second team), and the alias is the ONE line that keeps their queues apart
    (``QUEST_TEAM_ID = "CANTR_TEAM_ID"`` against a shared file that names the personal team). If the
    shared file ever grows a line under the library's own name, a file-first order would silently
    point the lane at the other team; the alias is the lane's explicit statement and it holds.
    """
    if file_cfg is None:
        return
    # What the process itself held before any file was read: the only thing an alias defers to.
    preset = {k for k, v in os.environ.items() if v}
    for path in (file_cfg.env_files or []):
        _load_env_file_truthy(str(path))

    for target, source in (file_cfg.env_aliases or {}).items():
        # Truthy, not presence: an .env carrying a blank placeholder under the library's own name
        # would otherwise read as "already set" and the aliased value would never apply. That
        # exact bug is why one lane's bridge helper had a four-line comment explaining itself.
        if str(target) in preset:
            continue
        value = os.environ.get(str(source), "")
        if value:
            os.environ[str(target)] = value

    for key, value in (file_cfg.env or {}).items():
        if not os.environ.get(str(key)) and str(value):
            os.environ[str(key)] = str(value)


def _load_env_file_truthy(path: str) -> None:
    """Load ``KEY=VALUE`` lines into ``os.environ``, treating a BLANK value as not set.

    Deliberately truthy-checked rather than presence-checked, which is the one behavioural
    difference from ``runner.lane.load_env_file``. A lane whose ``.env`` documents its optional
    credentials as blank lines (``QUEST_API_KEY=``, meaning "fall back to the shared file") is a
    real and reasonable shape, and a presence check reads those blanks as "already set", so the
    fallback file never applies and the lane starts unconfigured with nothing in the log to say
    why. That exact case is why one consumer carried a hand-written bridge helper with a four-line
    comment explaining itself.

    A missing file is a silent no-op: plenty of lanes read everything from the process environment.
    """
    p = Path(path)
    if not p.exists():
        return
    try:
        text = p.read_text()
    except OSError as e:
        _log.warning("config env_files: could not read %s: %s", path, e)
        return
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.split("#", 1)[0].strip().strip('"').strip("'")
        if key and val and not os.environ.get(key):
            os.environ[key] = val


def resolve_config_objects(cfg: RunnerConfig) -> RunnerConfig:
    """Turn the file's declarative pointers into the live objects and maps they describe.

    Each of these was, in every lane, a few lines of Python whose only job was to read a JSON file
    or construct one client. Resolved here instead, so a lane can be a config file and nothing
    else. Every step is best-effort and additive: a missing file or an unbuildable client logs and
    leaves the field as it was, exactly as an unconfigured optional feature does elsewhere.
    """
    # {quest_id: folder} from a JSON file, merged UNDER an inline map (inline wins).
    if cfg.quest_folder_map_file:
        loaded = _load_json_mapping(cfg.quest_folder_map_file, "quest_folder_map_file")
        if loaded:
            merged = {str(k): str(v) for k, v in loaded.items()}
            merged.update(cfg.quest_folder_map or {})
            cfg.quest_folder_map = merged

    # {quest_id: [spec, ...]} from a JSON file, same precedence.
    if cfg.context_sources_file:
        loaded = _load_json_mapping(cfg.context_sources_file, "context_sources_file")
        if loaded:
            merged = {str(k): v for k, v in loaded.items() if isinstance(v, list)}
            merged.update(cfg.context_sources_map or {})
            cfg.context_sources_map = merged

    # The Drive comments client, from a service-account credential. Left alone when a consumer
    # already supplied a live client in Python -- an explicit object always beats a description.
    if cfg.drive_comments is None and cfg.drive_comments_auth:
        cfg.drive_comments = _build_drive_comments(cfg.drive_comments_auth)

    # Same pattern, the inbound-mail client.
    if cfg.inbound_mail is None and cfg.inbound_mail_auth:
        cfg.inbound_mail = _build_inbound_mail(cfg.inbound_mail_auth)

    # Notion and Google Chat: same rule, an explicit live adapter beats a description, and a block
    # whose credentials or allowlist are missing logs why and wires nothing.
    if cfg.notion_adapter is None and cfg.notion:
        cfg.notion_adapter = _build_notion(cfg.notion)
    if cfg.google_chat_adapter is None and cfg.google_chat:
        cfg.google_chat_adapter = _build_google_chat(cfg.google_chat)

    if cfg.mcp_drive:
        _add_drive_mcp_server(cfg)

    if cfg.context_preamble_doctrine:
        from .core.context_doctrine import DEEP_CONTEXT_DOCTRINE
        own = (cfg.context_preamble or "").strip()
        if DEEP_CONTEXT_DOCTRINE not in own:
            cfg.context_preamble = DEEP_CONTEXT_DOCTRINE + ("\n" + own if own else "")
    return cfg


def resolve_context_assembler_stack(base_or_cfg: Any, cfg: Optional[RunnerConfig] = None, *,
                                    quest_client: Any = None) -> Any:
    """Fuse the semantic arm and the per-rep wrapper onto whatever assembler is configured.

    Called after the base assembler exists, because both of these WRAP it rather than replace it.
    Each layer is independent and each is skipped silently when unconfigured or unavailable, so a
    lane declaring both, one, or neither all work, and a missing optional dependency degrades to
    the layer below instead of failing the lane.
    """
    from .config import RunnerConfig  # lazy: config imports this module
    # Called two ways: with a built base plus its cfg (the resolver path), or with a cfg alone
    # (a consumer applying the layers to whatever it already put on cfg.context_assembler).
    if isinstance(base_or_cfg, RunnerConfig):
        cfg, base = base_or_cfg, base_or_cfg.context_assembler
    else:
        base = base_or_cfg
    if cfg is None or base is None or base is _AUTO_CONTEXT:
        return base
    if cfg.vector_store:
        base = _fuse_vector_arm(cfg, base)
    if cfg.rep_context_prefs:
        quest_client = quest_client or _quest_client_for(cfg)
    if cfg.rep_context_prefs and quest_client is not None:
        try:
            from .adapters.rep_context_assembler import RepContextAssembler
            base = RepContextAssembler(base, client=quest_client, team_id=cfg.team_id)
            _log.info("context: per-rep context_prefs layer ON")
        except Exception as e:  # noqa: BLE001 -- an optional layer never blocks a lane
            _log.warning("rep_context_prefs unavailable (%s); continuing without it", e)
    return base


def quest_client_from_config(cfg: RunnerConfig, *, timeout: float = 30.0) -> Any:
    """Build a ``QuestClient`` from an already-built ``RunnerConfig``.

    The one place the connection fields (+ decision routing) are turned into a client, so every
    consumer gets the same wiring instead of re-deriving it. Raises ``QuestNotConfigured`` when
    the URL or key is missing — see :func:`quest_ai_runner.load_client` for the config-file front
    door most callers want.
    """
    from .runner.quest_client import QuestClient, QuestNotConfigured

    if not (cfg.quest_base_url and cfg.quest_api_key):
        raise QuestNotConfigured(
            "quest_base_url and quest_api_key (qsk_...) are required. Set QUEST_BASE_URL and "
            "QUEST_API_KEY, or point QAR_CONFIG_FILE at a config file that supplies them "
            "(env_files/env_aliases map an existing .env onto those names).")
    return QuestClient(
        cfg.quest_base_url, cfg.quest_api_key, team_id=cfg.team_id, timeout=timeout,
        decision_assignees=cfg.decision_assignees,
        default_assignee_user_id=cfg.default_assignee_user_id,
        env_id=cfg.env_id,
    )


def load_quest_client(config_path: Optional[str] = None, *, timeout: float = 30.0) -> Any:
    """A ``QuestClient`` from a config file layered under the environment — no adapter stack.

    The lightweight sibling of :func:`quest_ai_runner.load_config`: it reads the same file (or
    ``QAR_CONFIG_FILE``), applies its ``env_files`` / ``env_aliases`` / ``env`` tables, and builds
    just the API client. Nothing here imports a model provider, a vector store or a context store,
    so a script that only wants to read a task or add a note starts instantly and needs none of
    the optional dependencies.

    Exported as ``quest_ai_runner.load_client``; see that name for the documented usage.
    """
    from .config import RunnerConfig  # lazy: config imports this module
    path = config_path or os.getenv("QAR_CONFIG_FILE") or ""
    file_cfg = RunnerConfig.from_file(path) if path else None
    apply_config_environment(file_cfg)
    base = file_cfg.quest_base_url if file_cfg else ""
    key = file_cfg.quest_api_key if file_cfg else ""
    team = file_cfg.team_id if file_cfg else ""
    # The environment wins over the file on every field, exactly as it does for a full lane
    # (cli._layer_file_defaults) — including the names apply_config_environment just populated
    # from the file's own env_files/env_aliases, which is how a key that lives only in a chmod-600
    # .env reaches a client whose config file holds no secret.
    cfg = RunnerConfig(
        quest_base_url=os.getenv("QUEST_BASE_URL") or base,
        quest_api_key=os.getenv("QUEST_API_KEY") or key,
        team_id=os.getenv("QUEST_TEAM_ID") or team,
        default_assignee_user_id=(os.getenv("QAR_DECISION_ASSIGNEE")
                                  or (file_cfg.default_assignee_user_id if file_cfg else None)),
        decision_assignees=(parse_decision_assignees(os.getenv("QAR_DECISION_ASSIGNEES"))
                            or (file_cfg.decision_assignees if file_cfg else {})),
    )
    return quest_client_from_config(cfg, timeout=timeout)


def parse_decision_assignees(raw: Optional[str]) -> Dict[str, str]:
    """Parse ``QAR_DECISION_ASSIGNEES`` — a JSON object, or ``name=id,name=id`` pairs.

    Both spellings because this variable is written in two very different places: a JSON object is
    natural in a systemd unit or a secrets manager, ``name=id`` pairs in a shell export. Returns
    ``{}`` for an empty/absent value; raises ``ConfigFileError`` on a value that parses to
    something other than a flat mapping, rather than dropping the routing silently.
    """
    text = (raw or "").strip()
    if not text:
        return {}
    if text.startswith("{"):
        try:
            data = json.loads(text)
        except ValueError as e:
            raise ConfigFileError(f"QAR_DECISION_ASSIGNEES is not valid JSON: {e}") from e
    else:
        data = {}
        for part in text.split(","):
            name, sep, value = part.partition("=")
            if not sep:
                raise ConfigFileError(
                    f"QAR_DECISION_ASSIGNEES entry {part.strip()!r} is not 'name=user_id' "
                    f"(use a JSON object for anything more complex)")
            data[name.strip()] = value.strip()
    if not isinstance(data, dict) or any(not isinstance(v, str) for v in data.values()):
        raise ConfigFileError(
            "QAR_DECISION_ASSIGNEES must be a flat {role: user_id} mapping of strings")
    return {str(k): str(v) for k, v in data.items()}


def _quest_client_for(cfg: RunnerConfig) -> Any:
    """A QuestClient built from this config, or None when it is not configured to reach Quest."""
    if not (cfg.quest_base_url and cfg.quest_api_key):
        return None
    try:
        return quest_client_from_config(cfg)
    except Exception as e:  # noqa: BLE001
        _log.warning("could not build a Quest client for the context stack (%s)", e)
        return None


def _fuse_vector_arm(cfg: RunnerConfig, keyword: Any) -> Any:
    """Fuse a Qdrant/embedding semantic arm with the keyword arm, or return the keyword arm.

    Guarded end to end and best-effort by design: the deps, the embedding key and the vector
    database are each things a deployment may not have ready, and none of them is worth failing a
    production lane over. A lane that cannot build the arm logs why and runs keyword-only.
    """
    spec = cfg.vector_store or {}
    try:
        from .adapters import HybridContextAssembler, VectorContextAssembler
        from .adapters.qdrant_vector_store import QdrantVectorStore, make_voyage_embedder

        store = QdrantVectorStore(
            url=str(spec.get("url") or "http://localhost:6333"),
            vector_size=int(spec.get("vector_size") or 1024),
            embedder=make_voyage_embedder(input_type="document"),
            query_embedder=make_voyage_embedder(input_type="query"),
            collection_prefix=str(spec.get("collection_prefix") or "qar_ctx"),
        )
        vector = VectorContextAssembler(
            store, provider=cfg.model_provider,
            seed_source=getattr(keyword, "export_for_embedding", None))
        _log.info("context: semantic vector arm ON (hybrid with the keyword arm)")
        return HybridContextAssembler(keyword=keyword, vector=vector)
    except Exception as e:  # noqa: BLE001 -- keyword-only is a correct lane, not a broken one
        _log.info("context: vector arm disabled, keyword-only (%s: %s)", type(e).__name__, e)
        return keyword


def _add_drive_mcp_server(cfg: RunnerConfig) -> None:
    """Fold a read-only Drive MCP server into ``cfg.mcp_servers`` when its key is on disk.

    The key-present check is the whole gate. Whether the Cloud APIs behind it are enabled is a
    separate ops step that may not be done: ``MCPClient`` degrades to a clean "could not connect"
    observation per its own never-raise contract, so this only decides whether Drive MCP is
    ATTEMPTED, on the one thing that is cheaply checkable here.
    """
    spec = cfg.mcp_drive or {}
    key_file = str(spec.get("service_account_file") or "").strip()
    if not key_file or not Path(key_file).is_file():
        _log.info("Drive MCP not wired: no readable service-account key at %r", key_file)
        return
    try:
        from .adapters.google_chat_adapter import service_account_token_provider
        from hubofhubs.act.mcp_client import MCPServerSpec

        alias = str(spec.get("alias") or "drive")
        if any(getattr(s, "alias", "") == alias for s in (cfg.mcp_servers or [])):
            return                          # already wired by a consumer; never a second copy
        cfg.mcp_servers = list(cfg.mcp_servers or []) + [MCPServerSpec(
            alias=alias,
            transport="http",
            url=str(spec.get("url") or ""),
            allowed_tools=tuple(spec.get("allowed_tools") or ()),
            token_provider=service_account_token_provider(
                service_account_file=key_file,
                subject=str(spec.get("subject") or "").strip() or None,
                scopes=[str(x) for x in (spec.get("scopes") or ())] or None),
        )]
        _log.info("Drive MCP wired (alias %s, read-only)", alias)
    except Exception as e:  # noqa: BLE001 -- optional wiring never breaks the lane
        _log.warning("Drive MCP wiring skipped (%s), continuing", e)
