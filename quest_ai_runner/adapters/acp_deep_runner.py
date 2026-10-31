"""ACP deep runner: QAR's binding of the hub's (``hubofhubs.deep.acp_runner``), a SECOND, opt-in ``DeepRunner`` that drives Claude
over the Agent Client Protocol instead of a one-shot ``claude -p`` subprocess (mid-turn steering is what it buys; the design notes
are in the hub module).

``AcpDeepRunner`` here is the hub's class with QAR's settings (``acp_deep_runner_config.acp_settings``: the ``QAR_ACP_*``
fallbacks, the goal settings and the ``Escalation`` value for a permission ask). It stays the ``run-deep-work`` implementation
named ``acp``, wired explicitly (``RunnerConfig(deep_runner=AcpDeepRunner(AcpConfig(...)))``). Every name of the
``acp_deep_runner_*`` modules stays importable from here.
"""
from __future__ import annotations

from esoi import implementation

from hubofhubs.deep.acp_config import AcpSettings
from hubofhubs.deep.acp_runner import AcpDeepRunner as HubAcpDeepRunner, acp_model_family  # noqa: F401

from .acp_deep_runner_config import (  # noqa: F401  (kept importable from here)
    ALLOW,
    AcpConfig,
    HUMAN,
    REJECT,
    acp_settings,
    build_agent_argv,
    classify_permission,
    permission_response,
    resolve_agent_entry,
    resolve_node_binary,
    select_option,
)
from .acp_deep_runner_protocol import (  # noqa: F401  (kept importable from here)
    ACP_MODEL_FAMILIES,
    ACP_PROTOCOL_VERSION,
    AcpUnavailable,
    EFFORT_CONFIG_ID,
    MODEL_CONFIG_ID,
    PHASE_DONE,
    PHASE_ERROR,
    PHASE_HEARTBEAT,
    PHASE_MESSAGE,
    PHASE_PLAN,
    PHASE_SESSION,
    PHASE_STEER,
    PHASE_THINKING,
    PHASE_TOOL_CALL,
    PHASE_TOOL_ERROR,
    PHASE_TOOL_PROGRESS,
    PHASE_TOOL_RESULT,
    QAR_ACP_AGENT_COMMAND_ENV_VAR,
    QAR_ACP_NODE_PATH_ENV_VAR,
    STEERING_EXT_METHOD,
    STOP_REASON_FAILURES,
    field_of,
    log,
    meta_of,
    tool_name_of,
    translate_session_update,
    truncate,
    usage_from_prompt_response,
)
from .acp_deep_runner_session import AcpRun, AcpSessionClient, open_agent_connection  # noqa: F401


@implementation("run-deep-work", "acp")
class AcpDeepRunner(HubAcpDeepRunner):
    """A ``DeepRunner`` that runs each goal as one ACP session against the Claude ACP agent, with QAR's settings."""

    config_class = AcpConfig   # the dataclass this runner is built from (core.resolve.resolve_config)

    def __init__(self, config: AcpConfig):
        super().__init__(config)

    def acp_settings(self) -> AcpSettings:
        """QAR's settings, built from the environment on every call."""
        return acp_settings()
