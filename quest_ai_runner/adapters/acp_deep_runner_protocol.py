"""ACP deep runner, wire vocabulary: the hub's (``hubofhubs.deep.acp_protocol``) under the old import path.

Protocol constants, the phase names of the progress vocabulary, tolerant field access over SDK payloads, the translation of one
``session/update`` into an ``EVENT_EXEC`` payload, usage extraction and the stop-reason verdicts are the hub's. What stays with
QAR is the names of its two environment fallbacks.
"""
from __future__ import annotations

from hubofhubs.deep.acp_protocol import (  # noqa: F401  (same objects, old import path)
    ACP_MODEL_FAMILIES,
    ACP_PROTOCOL_VERSION,
    DEFAULT_AGENT_BINARY,
    EFFORT_CONFIG_ID,
    FILE_CHANGE_KINDS,
    FILE_CHANGE_TOOLS,
    MAX_MESSAGE_CHARS,
    MAX_THOUGHT_CHARS,
    MIN_NODE_MAJOR,
    MISSING,
    MODEL_CONFIG_ID,
    PHASE_DONE,
    PHASE_ERROR,
    PHASE_ESCALATED,
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
    STEERING_EXT_METHOD,
    STEERING_IDLE_BEHAVIOR,
    STOP_REASON_FAILURES,
    TOOL_COMPLETED,
    TOOL_STATUS_PHASES,
    AcpUnavailable,
    chunk_text,
    field_of,
    landed_file_change,
    log,
    meta_of,
    tool_line,
    tool_name_of,
    translate_session_update,
    truncate,
    usage_from_prompt_response,
    usage_from_update,
)

# Env fallbacks used ONLY when the consumer left the matching AcpConfig field unset: config wins, env is the floor.
QAR_ACP_NODE_PATH_ENV_VAR = "QAR_ACP_NODE_PATH"
QAR_ACP_AGENT_COMMAND_ENV_VAR = "QAR_ACP_AGENT_COMMAND"
