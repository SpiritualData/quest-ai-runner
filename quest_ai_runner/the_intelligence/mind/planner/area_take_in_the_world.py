"""``take-in-the-world``: a person or a program reaches the runner from wherever they work and is understood the same way.

The area arm ``ways_in`` takes the ways in as typed seats and offers the area's one line, ``take_in(message)``: the route the message
takes. The members are the message router (Planner) and the live channel (Intake Sources), built on first use.
"""
from __future__ import annotations

import contextlib
import io
from typing import Any, Mapping, Sequence

from esoi import implementation
from esoi import Lazy

from quest_ai_runner.the_consciousness.request_intake.interface import ChannelTransport
from quest_ai_runner.the_intelligence.mind.planner.interface import MessageRouting
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


@implementation("quest-requests", "ways_in")
class WaysIn:
    """Each way in hands its request to the same router. ``take_in`` returns the route of the message and the sources that feed its
    context; the other methods hand a request to the member that owns that way in."""
    REQUIRES = ()     # the seats it is handed

    def __init__(self, routes: MessageRouting, channels: Lazy[ChannelTransport]) -> None:
        self.routes, self.channels = routes, channels

    def take_in(self, message: str) -> Mapping[str, Any]:
        return {"route": self.routes.route(message)}

    def reply(self, reply: Any) -> Any:
        return self.channels.send(reply)
