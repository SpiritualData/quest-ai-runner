"""ACP deep runner, configuration and policy: the hub's (``hubofhubs.deep.acp_config``) with QAR's environment and escalation.

Node and agent-binary discovery, ``AcpConfig`` and the pure permission mapping are the hub's. What stays with QAR:

* the environment fallbacks ``QAR_ACP_NODE_PATH`` and ``QAR_ACP_AGENT_COMMAND`` (``acp_settings`` reads them on every call and
  hands them to the hub as values, with their names for the messages);
* ``AcpConfig.escalation_quest_id``, the quest an escalated permission ask is attached to, and the ``Escalation`` value the
  escalation sink receives (``make_escalation``).
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, List, Optional, Tuple

from hubofhubs.deep import acp_config as hub
from hubofhubs.deep.acp_config import (  # noqa: F401  (same objects, old import path)
    ALLOW,
    HUMAN,
    REJECT,
    AcpSettings,
    classify_permission,
    node_major_version,
    permission_response,
    select_option,
)

from ..core.adapters_values import Escalation
from ..core.goal_runner_contract import goal_settings
from .acp_deep_runner_protocol import (
    MIN_NODE_MAJOR,
    QAR_ACP_AGENT_COMMAND_ENV_VAR,
    QAR_ACP_NODE_PATH_ENV_VAR,
)


@dataclass
class AcpConfig(hub.AcpConfig):
    """Config for the ACP deep runner: the hub's, plus the quest an escalated permission ask belongs to."""

    escalation_quest_id: Optional[str] = None


def make_escalation(*, cfg: Any, summary: str, kind: str, assignee: Optional[str], default_on_silence: str) -> Escalation:
    """The ``Escalation`` an ACP permission ask becomes, attached to ``cfg.escalation_quest_id``."""
    return Escalation(summary=summary, kind=kind, quest_id=getattr(cfg, "escalation_quest_id", None),
                      assignee=assignee, default_on_silence=default_on_silence)


def acp_settings() -> AcpSettings:
    """The hub's ACP settings, built from the environment in force right now and QAR's goal settings."""
    return AcpSettings(
        goal=goal_settings(),
        node_path_fallback=os.getenv(QAR_ACP_NODE_PATH_ENV_VAR),
        agent_command_fallback=os.getenv(QAR_ACP_AGENT_COMMAND_ENV_VAR),
        node_path_name=QAR_ACP_NODE_PATH_ENV_VAR,
        agent_command_name=QAR_ACP_AGENT_COMMAND_ENV_VAR,
        make_escalation=make_escalation,
    )


def resolve_node_binary(configured: Optional[str] = None) -> Optional[str]:
    """The ``node`` used to launch the ACP agent: config, then ``QAR_ACP_NODE_PATH``, then PATH."""
    return hub.resolve_node_binary(configured, acp_settings())


def resolve_agent_entry(configured: Optional[str] = None) -> Optional[str]:
    """The ACP agent program: config, then ``QAR_ACP_AGENT_COMMAND``, then ``claude-agent-acp`` on PATH."""
    return hub.resolve_agent_entry(configured, acp_settings())


def build_agent_argv(
    *,
    agent_command: Optional[str],
    agent_args: Optional[List[str]],
    node_path: Optional[str],
    min_node_major: int = MIN_NODE_MAJOR,
) -> Tuple[List[str], Optional[str]]:
    """The argv that launches the ACP agent, or an explanatory error (see ``hubofhubs.deep.acp_config.build_agent_argv``)."""
    return hub.build_agent_argv(agent_command=agent_command, agent_args=agent_args, node_path=node_path,
                                min_node_major=min_node_major, settings=acp_settings())
