"""Strip a runner's internal scaffolding and inline reasoning markup out of text that reaches a user.

The orchestrator asks every deep worker to PREPARE FUTURE CONTEXT (bullets the async card updater learns
from). That ask is appended to a brief, and a worker may emit the matching section in its output. Neither
may reach a user, and neither may corrupt a strict payload (generated code, JSON) a worker returns. This
module is the consumer-side safety net the library owns so no consumer copies it:

  * ``strip_future_context`` cuts the ask and any emitted section off the tail of a text.
  * ``strip_reasoning_markup`` removes inline ``<think>`` style reasoning blocks (fenced code is left alone).
  * ``sanitize_user_text`` applies both at the reply boundary.

It is a safety net, not the mechanism: a runner that declares the out-of-band field channel
(``future_context_channel``) is asked in a way that never names the delimiter.
"""
from __future__ import annotations

import re
from typing import Optional

# The delimiter the library asks deep workers to emit, imported so a rename cannot defeat this strip.
from .orchestrator import FUTURE_CONTEXT_DELIMITER

# Every shape of the scaffolding, matched loosely (the exact rule text may drift in the library):
#   * the OUTPUT-channel ask appended to a brief:  "--- PREPARE FUTURE CONTEXT ---"
#   * the FIELD-channel ask appended to a brief:   "--- PREPARE FUTURE CONTEXT (OUT OF BAND) ---"
#     (anything after the header words is tolerated, so a reworded parenthetical still matches)
#   * the SECTION a model emits in response:       "=== FUTURE CONTEXT (for ...) ==="
# Matched at line starts so ordinary prose that merely mentions future context is never cut.
SCAFFOLDING_MARKER = re.compile(
    r"(?im)^[ \t]*(?:-{2,}[ \t]*PREPARE FUTURE CONTEXT\b.*|={2,}[ \t]*FUTURE CONTEXT\b.*)[ \t]*$"
)

# The tag names models actually wrap their own reasoning in. Kept tight on purpose: these are
# reasoning wrappers, not every angle-bracketed word a reply might contain. Mirrored by a
# consumer's UI, which does the same job at render time for streamed text.
REASONING_TAGS = ("think", "thinking", "reasoning", "thought", "thoughts", "scratchpad")
_TAG_ALT = "|".join(REASONING_TAGS)
# A complete block, and (for a reply cut off mid-reasoning) an opener with nothing closing it.
REASONING_BLOCK = re.compile(rf"(?is)<\s*({_TAG_ALT})\b[^>]*>.*?<\s*/\s*\1\s*>")
REASONING_OPEN_TAIL = re.compile(rf"(?is)<\s*(?:{_TAG_ALT})\b[^>]*>.*$")
REASONING_STRAY_CLOSE = re.compile(rf"(?i)<\s*/\s*(?:{_TAG_ALT})\s*>")


def strip_future_context(text: Optional[str]) -> str:
    """Return ``text`` with the library's future-context scaffolding (and everything after it) gone.

    The scaffolding is always a TAIL: the library appends its ask at the end of a brief, and tells
    the model to END its output with the emitted section. So cutting from the first marker to the end
    removes exactly the internal block while keeping the real content in front of it. Text with no
    marker is returned unchanged (apart from trailing whitespace). Never raises.

    THIS IS A SAFETY NET, NOT THE MECHANISM. Applied to generated code before its syntax check, to
    brief-derived text headed for a user-facing prompt, and (only on a library with no out-of-band
    field channel) to the codegen brief itself. It is NOT applied to the brief on a current library:
    the FIELD ask must reach the code generator, because its answer is what the card updater learns
    from. See the module docstring.
    """
    if not text:
        return text or ""
    match = SCAFFOLDING_MARKER.search(text)
    if not match:
        return text
    return text[: match.start()].rstrip()


def strip_reasoning_markup(text: Optional[str]) -> str:
    """Return ``text`` with any inline model-reasoning block removed.

    A provider-level thinking BLOCK cannot reach a reply: every provider adapter assembles the
    answer from text blocks only. What does reach it is reasoning a model writes INLINE into its own
    answer as markup, "<think>the user wants X, so first I should ...</think>", which nothing
    downstream removed, so the user read the model's working instead of its answer. The reply-voice
    prompt forbids it, but a prompt is a request, not a guarantee: this is the guarantee.

    Fenced code is left untouched, so a reply that legitimately shows a <think> tag inside a fence
    still renders it. Never raises.
    """
    if not text or "<" not in text:
        return text or ""
    # Odd-indexed segments are inside a fence; only clean the even ones.
    segments = text.split("```")
    for i in range(0, len(segments), 2):
        cleaned = REASONING_BLOCK.sub("", segments[i])
        cleaned = REASONING_OPEN_TAIL.sub("", cleaned)
        segments[i] = REASONING_STRAY_CLOSE.sub("", cleaned)
    out = "```".join(segments)
    if out == text:
        return text
    return re.sub(r"\n{3,}", "\n\n", out).strip()


def sanitize_user_text(text: Optional[str]) -> str:
    """The REPLY BOUNDARY guard: internal scaffolding must never reach a user-visible message.

    Applied to every reply a consumer hands to its chat surface (streamed result events and the
    non-streaming reply text). Defensive on purpose: the library may legitimately use the block
    internally, and a future library change could route it somewhere new, but a user must never read
    "=== FUTURE CONTEXT (for similar requests by this user) ===" with their quest ids under it.

    It guards the same boundary against the model's own inline reasoning markup. A consumer's UI should strip
    that too (one render-time choke point per chat surface, because a STREAMED reply is assembled in
    the client and never passes through here token by token); this keeps what gets PERSISTED and
    what a non-streaming caller reads clean at the source.
    """
    return strip_reasoning_markup(strip_future_context(text))
