"""``take-in-the-world``: a person or a program reaches the runner from wherever they work and is understood the same way.

The area arm ``ways_in`` takes the ways in as typed seats and offers the area's one line, ``take_in(message)``: the route the message
takes and the channels its context is fed from. The members are the message router (Planner), the live channel (Intake Sources), input that
arrives while a run works (``receive-input-mid-run``), the sources that feed a card's context (``feed-context-sources``) and attachments
(``describe-an-attachment``), each built on first use.
"""
from __future__ import annotations

from typing import Any, Mapping

from esoi import implementation
from esoi import Lazy

from quest_ai_runner.the_body.intake_sources.interface import ContextFeeding
from quest_ai_runner.the_consciousness.request_intake.interface import ChannelTransport
from quest_ai_runner.the_intelligence.mind.context_intake.attachments import AttachmentPreparing
from quest_ai_runner.the_intelligence.mind.delegation.interface import InputInbox
from quest_ai_runner.the_intelligence.mind.planner.interface import MessageRouting


@implementation("take-in-the-world", "ways_in")
class WaysIn:
    """Each way in hands its request to the same router. ``take_in`` returns the route of the message and the sources that feed its
    context; the other methods hand a request to the member that owns that way in."""
    REQUIRES = ()     # the seats it is handed

    def __init__(self, routes: MessageRouting, channels: Lazy[ChannelTransport], inbox: Lazy[InputInbox], feeds: Lazy[ContextFeeding],
                 attachments: Lazy[AttachmentPreparing]) -> None:
        self.routes, self.channels, self.inbox, self.feeds, self.attachments = routes, channels, inbox, feeds, attachments

    def take_in(self, message: str) -> Mapping[str, Any]:
        return {"route": self.routes.route(message), "sources": sorted(self.feeds.describe_sources())}

    def reply(self, reply: Any) -> Any:
        return self.channels.send(reply)

    def hold(self, conversation_id: str, message: str) -> None:
        self.inbox.push(conversation_id, message)

    def prepare(self, attachments: Any) -> Any:
        return self.attachments.prepare(attachments)
