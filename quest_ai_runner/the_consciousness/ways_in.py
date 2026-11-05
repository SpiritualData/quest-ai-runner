"""``quest-requests``: a person or a program reaches the runner from wherever they work and is understood the same way.

The area arm ``ways_in`` takes the ways in as typed seats and offers the area's one line, ``take_in(message)``: the route the message
takes. The members are the message router (Planner) and the live channel (Intake Sources), built on first use.
"""
from __future__ import annotations

from typing import Any, Mapping

from esoi import implementation
from esoi import Lazy

from quest_ai_runner.the_consciousness.request_intake.interface import ChannelTransport
from quest_ai_runner.the_intelligence.mind.planner.interface import MessageRouting


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
