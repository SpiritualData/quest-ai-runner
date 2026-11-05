"""``take-in-the-world``: a person or a program reaches the runner from wherever they work and is understood the same way.

The area arm ``ways_in`` takes the ways in as typed seats and offers the area's one line, ``take_in(message)``: the route the message
takes and the channels its context is fed from. The members are the message router (``route-a-message``), the live channel
(``talk-over-a-channel``), input that arrives while a run works (``receive-input-mid-run``), the sources that feed a card's context
(``feed-context-sources``), attachments (``describe-an-attachment``), the terminal (``operate-from-a-terminal``,
``assist-a-terminal-session``) and the package itself (``expose-the-public-api``). Members the line does not need are built on first use.
"""
from __future__ import annotations

import contextlib
import io
import typing
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from esoi import implementation, solution
from esoi.assemble import Lazy

from quest_ai_runner.the_body.configuration.cli_config import TerminalHelping
from quest_ai_runner.the_consciousness.request_intake.interface import ChannelTransport
from quest_ai_runner.the_intelligence.mind.context_intake.attachments import AttachmentPreparing
from quest_ai_runner.the_intelligence.mind.delegation.inbox import InputInbox
from quest_ai_runner.the_intelligence.mind.planner.message_routing import MessageRouting, RequestTakingIn
from quest_ai_runner.the_body.intake_sources.context_updates import ContextFeeding


@dataclass(frozen=True)
class TerminalRun:
    """What one command of the terminal did: its exit code and everything it printed."""
    code: int
    output: str


@solution("operate-from-a-terminal", kind="event", entry="operate", example=["--help"])
@typing.runtime_checkable
class TerminalOperating(typing.Protocol):
    """Runs one command of ``quest-ai-runner`` as the shell would, and returns what it did."""

    def operate(self, argv: Sequence[str], entry: str = "qar") -> TerminalRun: ...


@implementation("operate-from-a-terminal", "cli_commands")
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


@solution("expose-the-public-api", kind="event", entry="front_door", example="run")
@typing.runtime_checkable
class PublicSurface(typing.Protocol):
    """Names what the package offers a consumer and hands over the front door asked for (``run``, ``load_config``, ``load_client``)."""

    def front_door(self, name: str) -> Any: ...


@implementation("expose-the-public-api", "package_front_doors")
class PackageFrontDoors:
    """The names in the package's ``__all__``: ``front_door`` returns the one asked for and refuses a name the package does not offer."""
    REQUIRES = ()     # reads the package, nothing else

    def front_door(self, name: str) -> Any:
        import quest_ai_runner
        if name not in quest_ai_runner.__all__:
            raise KeyError(f"quest_ai_runner offers {', '.join(quest_ai_runner.__all__)}, not {name!r}")
        return getattr(quest_ai_runner, name)


@implementation("take-in-the-world", "ways_in")
class WaysIn:
    """Each way in hands its request to the same router. ``take_in`` returns the route of the message and the sources that feed its
    context; the other methods hand a request to the member that owns that way in."""
    REQUIRES = ()     # the seats it is handed

    def __init__(self, routes: MessageRouting, channels: Lazy[ChannelTransport], inbox: Lazy[InputInbox], feeds: Lazy[ContextFeeding],
                 attachments: Lazy[AttachmentPreparing], terminal: Lazy[TerminalOperating], helpers: Lazy[TerminalHelping],
                 surface: Lazy[PublicSurface]) -> None:
        self.routes, self.channels, self.inbox, self.feeds = routes, channels, inbox, feeds
        self.attachments, self.terminal, self.helpers, self.surface = attachments, terminal, helpers, surface

    def take_in(self, message: str) -> Mapping[str, Any]:
        return {"route": self.routes.route(message), "sources": sorted(self.feeds.describe_sources())}

    def reply(self, reply: Any) -> Any:
        return self.channels.send(reply)

    def hold(self, conversation_id: str, message: str) -> None:
        self.inbox.push(conversation_id, message)

    def prepare(self, attachments: Any) -> Any:
        return self.attachments.prepare(attachments)

    def operate(self, argv: Sequence[str], entry: str = "qar") -> TerminalRun:
        return self.terminal.operate(argv, entry)

    def judge_goal(self, goal: Mapping[str, Any]) -> Any:
        return self.helpers.judge_goal(dict(goal))

    def front_door(self, name: str) -> Any:
        return self.surface.front_door(name)
