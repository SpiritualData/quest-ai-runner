"""The interface of write-through-mcp: Protocols declared with ``@solution``. Importing this module runs nothing."""
from __future__ import annotations

import typing
from esoi import solution
from hubofhubs.api import OperationWriter


@solution("write-through-mcp", kind="event", entry="write_operation", example={"name": "issues:create", "args": {"title": "Draft the launch note"}})
@typing.runtime_checkable
class OperationWriting(OperationWriter, typing.Protocol):
    """The hub's operation-writer interface (a named, schema-described mutating operation, reported as a value) over the MCP servers a lane connects."""
