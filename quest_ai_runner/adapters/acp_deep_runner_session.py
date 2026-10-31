"""ACP deep runner, the live session: the hub's (``hubofhubs.deep.acp_session``) under the old import path.

``open_agent_connection`` (the only place the optional ``acp`` SDK is touched), ``AcpRun`` and ``AcpSessionClient`` are the hub's.
The ``Escalation`` an ask becomes is built by the factory in ``acp_deep_runner_config.acp_settings``.
"""
from __future__ import annotations

from hubofhubs.deep.acp_session import AcpRun, AcpSessionClient, open_agent_connection  # noqa: F401  (same objects, old import path)
