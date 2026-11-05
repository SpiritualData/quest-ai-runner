"""The terminal's two arms of Integrity: the command dispatcher run in process and the front doors the package offers a consumer."""
from __future__ import annotations

import contextlib
import io
from typing import Any, Sequence

from esoi import implementation

from quest_ai_runner.the_consciousness.terminal.interface import TerminalRun



@implementation("system-integrity", "cli_commands")
class CliCommands:
    """The command dispatchers called in process, with their output and exit code captured: ``qar`` is ``quest-ai-runner`` (``cli.main``)
    and ``tools`` is ``python -m quest_ai_runner tools`` (list, show, call)."""
    REQUIRES = ()     # the dispatcher reads its own environment when a command runs

    def operate(self, argv: Sequence[str], entry: str = "qar") -> TerminalRun:
        from quest_ai_runner.the_consciousness.terminal import cli
        from quest_ai_runner.the_consciousness.terminal import __main__ as tools_main
        dispatch = {"qar": cli.main, "tools": tools_main.main}[entry]
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
            try:
                code = dispatch(list(argv))
            except SystemExit as stop:
                code = stop.code if isinstance(stop.code, int) else (0 if stop.code is None else 1)
        return TerminalRun(code=int(code or 0), output=buffer.getvalue())



@implementation("system-integrity", "package_front_doors")
class PackageFrontDoors:
    """The names in the package's ``__all__``: ``front_door`` returns the one asked for and refuses a name the package does not offer."""
    REQUIRES = ()     # reads the package, nothing else

    def front_door(self, name: str) -> Any:
        import quest_ai_runner
        if name not in quest_ai_runner.__all__:
            raise KeyError(f"quest_ai_runner offers {', '.join(quest_ai_runner.__all__)}, not {name!r}")
        return getattr(quest_ai_runner, name)
