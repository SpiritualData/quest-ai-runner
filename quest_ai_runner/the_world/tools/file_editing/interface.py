"""The interface of FileWriting: Protocols declared with ``@solution``. Importing this module runs nothing."""
from __future__ import annotations

import typing
from esoi import solution
from hubofhubs.api import FileWriter


@solution("file-editing", kind="event", entry="read_file", example="NOTES.md")
@typing.runtime_checkable
class FileWriting(FileWriter, typing.Protocol):
    """Changes the text files under one root and keeps what it replaced; a file outside the root is refused."""
