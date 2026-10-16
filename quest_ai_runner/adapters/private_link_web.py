"""PrivateLinkWebResearch: a live-web adapter that falls back to a PRIVATE link reader.

``WebResearchAdapter.fetch`` can only open what the public internet can: a Google Doc, Sheet,
Slides or Drive file that is not shared with "anyone with the link" fails with ``not_shared``. A
consumer that has somewhere able to open it AS THE REQUESTING PERSON (the ``link`` operation of a
``serve-files`` server, see ``google_link_reader``) wraps its web adapter in this class, per turn,
with a ``private_link_reader`` closure bound to that person.

The fallback is structural: after a Google link fails publicly, the reader is asked, whatever the
planner chose, so the model needs no extra read key and cannot forget to use it. The model sees one
ordinary ``web_page`` read that worked, or the reader's own ``LINK NOT READ [reason]`` sentence
(for example "not shared with your account"). A private result is never written to the shared web
cache: it belongs to one person.

Everything else (search, backend name, describe, ...) is the wrapped adapter's own.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Optional, Sequence

from ..core.adapters import Observation
from .google_link_reader import parse_google_link

log = logging.getLogger("quest-ai-runner.private-link-web")

#: ``(url, *, full, part, focus) -> Observation | None``. ``None`` means "I cannot read links here",
#: so the next reader (or the public error) stands.
PrivateLinkReader = Callable[..., Optional[Observation]]


class PrivateLinkWebResearch:
    """Wrap a web adapter so a Google link that cannot be read publicly is read privately."""

    def __init__(self, web: Any, readers: Sequence[PrivateLinkReader]) -> None:
        self.web = web
        self.readers = list(readers)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.web, name)

    def fetch(self, url: str, *, focus: Optional[str] = None, fresh: bool = False,
              full: bool = False, part: Optional[int] = None) -> Observation:
        public = self.web.fetch(url, focus=focus, fresh=fresh, full=full, part=part)
        if public.kind != "error" or not self.readers or parse_google_link(url) is None:
            return public
        for reader in self.readers:
            try:
                private = reader(url, full=full, part=part, focus=focus)
            except Exception as exc:  # noqa: BLE001 - a reader must never break the loop
                log.warning("private link reader failed: %s", type(exc).__name__)
                continue
            if private is not None:
                return private
        return public
