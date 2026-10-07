"""The deep worker's prompt budget: one explicit token budget, spent by priority.

WHY THIS EXISTS (incident, 2026-10-06). Autopilot work threads on the SD shared lane failed with
"The deep worker could not start: Prompt is too long", and the failure was mailed to the quest's
people. The composed prompt of one daily pass had grown from 367K characters (2 October) to 548K
(4 October) to 838K (6 October), because every producer added its piece with nothing measuring the
whole: retrieved past conversations quoted earlier composed briefs verbatim (each carrying its own
context-updates block and receipt gate, sixteen blocks in one prompt), the request text rode twice,
and the thread's resumed session replayed every earlier pass on top. Each producer had its own
local cap or none, and no single place could say "this is too much, cut the least useful part".

This module is that single place. It owns:

  * ``estimate_tokens``: the one token estimate every budget here is measured in.
  * ``Section`` and ``fit_sections``: a prompt as an ordered list of named sections, each with a
    PRIORITY. When the whole is over budget, the lowest-priority material is compressed first,
    then dropped, and only when nothing else is left is the request itself clipped. Every cut is
    returned (and logged by callers) so a run that saw less than everything says so.
  * ``clip``: the one way text is shortened, always with a visible marker, so a reader never
    mistakes a cut block for the whole of it. Per-item caps elsewhere (the last-run excerpt in the
    autopilot brief, for one) clip through this too, so there is one notion of "cut".
  * ``keep_last_block``: exactly one context-updates block per prompt. The newest is the one that
    is live; every earlier copy is replaced by a one-line note.
  * ``fit_deep_prompt``: the deep worker's assembled prompt (preamble blocks, the TASK, the GOAL)
    fitted to the budget, with the preamble split on the block headers this library itself emits.
  * The budget and the model window: ``resolve_deep_prompt_budget`` picks the budget (explicit
    value, else ``QAR_DEEP_PROMPT_TOKEN_BUDGET``, else the default) and never lets it exceed what
    the chosen model's window can hold next to the worker's own system prompt and working room.

PRIORITIES, highest first (lower number is kept longer):

  0 ``PRIORITY_REQUEST``   standing instructions and today's request, the result contract
  1 ``PRIORITY_STANDING``  doctrine, persona, learned corrections, the lane's org preamble
  2 ``PRIORITY_UPDATES``   fresh updates: what changed since an assistant last looked
  3 ``PRIORITY_GOALS``     the person's goal frame
  4 ``PRIORITY_PLAN``      plan of record (QUEST_SYNC next steps), what the brain read for this goal
  5 ``PRIORITY_HISTORY``   earlier runs, last run, previous period, past conversations
  6 ``PRIORITY_RETRIEVAL`` retrieval cards (keyword, vector, recent turns, rep habits)

Pure functions, no I/O, never raises on odd input.
"""
from __future__ import annotations

import logging
import math
import os
import re
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Tuple

log = logging.getLogger("quest-ai-runner.prompt_budget")

# Characters per token for the estimate. English prose runs near 4; the material here is dense
# with ids, links and paths, which tokenise worse, so the estimate is deliberately conservative: a
# budget that over-counts leaves room, one that under-counts launches a run that dies.
CHARS_PER_TOKEN = 3.5

# The context window a Claude model is assumed to have when nothing says otherwise, and the
# larger one a ``[1m]`` model id asks for.
DEFAULT_CONTEXT_WINDOW_TOKENS = 200_000
LONG_CONTEXT_WINDOW_TOKENS = 1_000_000

# What the worker needs beside our prompt inside that window: Claude Code's own system prompt and
# tool definitions, the CLAUDE.md memory files of the folder it starts in, and room to read files
# and think. Our prompt may use at most the window minus this.
WORKER_RESERVED_TOKENS = 80_000

# The default budget for the deep worker's prompt. About 210K characters: comfortably more than a
# healthy autopilot pass needs (about 60K characters of brief plus selected context), and well
# under any window, so the budget only bites when something has started to accumulate.
DEFAULT_DEEP_PROMPT_TOKEN_BUDGET = 60_000
DEEP_PROMPT_BUDGET_ENV = "QAR_DEEP_PROMPT_TOKEN_BUDGET"

# The smallest budget anything here will shrink to, so a retry that halves the budget cannot
# halve it into uselessness.
MIN_DEEP_PROMPT_TOKEN_BUDGET = 8_000

PRIORITY_REQUEST = 0
PRIORITY_STANDING = 1
PRIORITY_UPDATES = 2
PRIORITY_GOALS = 3
PRIORITY_PLAN = 4
PRIORITY_HISTORY = 5
PRIORITY_RETRIEVAL = 6

# How far a section is first compressed before it is dropped outright, as a share of what it was
# (never below ``COMPRESS_FLOOR_CHARS``): a shortened past conversation is often enough, and
# keeping its head is cheaper than losing it entirely.
COMPRESS_SHARE = 0.25
COMPRESS_FLOOR_CHARS = 600

CUT_MARKER = "[... cut here to fit this run's prompt budget; {n} characters left out]"


def estimate_tokens(text: Optional[str]) -> int:
    """A conservative token estimate for ``text`` (see ``CHARS_PER_TOKEN``)."""
    if not text:
        return 0
    return int(math.ceil(len(text) / CHARS_PER_TOKEN))


def tokens_to_chars(tokens: int) -> int:
    """The character length ``tokens`` buys under the same estimate."""
    return max(0, int(tokens * CHARS_PER_TOKEN))


def clip(text: Optional[str], max_chars: int, *, marker: Optional[str] = None) -> str:
    """``text`` shortened to about ``max_chars``, cut at a line or word boundary, with a marker.

    ``marker`` replaces the default note (it may carry ``{n}``, the number of characters left out).
    Text already within the limit comes back unchanged, byte for byte.
    """
    body = text or ""
    if max_chars <= 0:
        max_chars = 0
    if len(body) <= max_chars:
        return body
    head = body[:max_chars]
    # Prefer a clean cut: the last line break, else the last space, in the final fifth.
    for sep in ("\n", " "):
        at = head.rfind(sep)
        if at >= int(max_chars * 0.8):
            head = head[:at]
            break
    left_out = len(body) - len(head)
    note = (marker or CUT_MARKER)
    try:
        note = note.format(n=left_out)
    except (KeyError, IndexError, ValueError):
        pass
    return head.rstrip() + "\n\n" + note


@dataclass
class Section:
    """One named piece of a prompt.

    ``required`` sections are never dropped; they are clipped only when every optional section is
    already gone and the prompt is still over budget. ``floor_chars`` is how short compression may
    make an optional section before dropping it (None means ``COMPRESS_SHARE`` of its size).
    ``drop_note`` replaces a dropped section, so the reader knows something was there.
    """
    name: str
    text: str
    priority: int = PRIORITY_RETRIEVAL
    required: bool = False
    floor_chars: Optional[int] = None
    drop_note: Optional[str] = None


@dataclass
class Cut:
    """What fitting did to one section: ``compressed``, ``dropped`` or ``clipped`` (required)."""
    name: str
    action: str
    tokens_before: int
    tokens_after: int

    def describe(self) -> str:
        return f"{self.name} {self.action} ({self.tokens_before} -> {self.tokens_after} tokens)"


@dataclass
class FitResult:
    """The fitted sections (in their original order) and every cut made to get there."""
    sections: List[Section]
    cuts: List[Cut] = field(default_factory=list)
    budget_tokens: int = 0
    tokens_before: int = 0
    tokens_after: int = 0

    def text(self, joiner: str = "\n\n") -> str:
        return joiner.join(s.text for s in self.sections if s.text)

    def summary(self) -> str:
        if not self.cuts:
            return ""
        return (f"prompt budget {self.budget_tokens} tokens: {self.tokens_before} -> "
                f"{self.tokens_after}; " + "; ".join(c.describe() for c in self.cuts))


def total_tokens(sections: Iterable[Section], joiner_tokens: int = 1) -> int:
    items = [s for s in sections if s.text]
    return sum(estimate_tokens(s.text) for s in items) + joiner_tokens * max(0, len(items) - 1)


def fit_sections(sections: List[Section], budget_tokens: int) -> FitResult:
    """Fit ``sections`` into ``budget_tokens``, cutting the lowest-priority material first.

    Order of cuts, stopping as soon as the whole fits:
      1. optional sections, one priority tier at a time from the lowest: the tier's sections are
         compressed to their floor (largest first), then dropped (replaced by their
         ``drop_note`` when they have one);
      2. only when every optional section is gone, required sections, largest first, are clipped
         down to what is left.
    The relative order of the sections is never changed.
    """
    work = [Section(s.name, s.text or "", s.priority, s.required, s.floor_chars, s.drop_note)
            for s in sections]
    before = total_tokens(work)
    result = FitResult(sections=work, budget_tokens=budget_tokens, tokens_before=before)
    if budget_tokens <= 0 or before <= budget_tokens:
        result.tokens_after = before
        return result

    optional = [s for s in work if not s.required and s.text]

    def over() -> int:
        return total_tokens(work) - budget_tokens

    # One priority tier at a time, lowest first: every section of the tier is compressed (largest
    # first), then, if that was not enough, dropped. A higher tier is touched only when everything
    # below it is already gone, so fresh updates are never shortened to keep an old card.
    for prio in sorted({s.priority for s in optional}, reverse=True):
        if over() <= 0:
            break
        tier = sorted((s for s in optional if s.priority == prio), key=lambda s: -len(s.text))
        for s in tier:                                # 1. compress
            if over() <= 0:
                break
            floor = s.floor_chars if s.floor_chars is not None else max(
                COMPRESS_FLOOR_CHARS, int(len(s.text) * COMPRESS_SHARE))
            if len(s.text) <= floor + 200:
                continue
            # The 200 leaves room for the cut marker itself.
            target = max(floor, len(s.text) - tokens_to_chars(over()) - 200)
            b = estimate_tokens(s.text)
            s.text = clip(s.text, target)
            result.cuts.append(Cut(s.name, "compressed", b, estimate_tokens(s.text)))
        for s in tier:                                # 2. drop
            if over() <= 0:
                break
            if not s.text or (s.drop_note and s.text == s.drop_note):
                continue
            b = estimate_tokens(s.text)
            s.text = s.drop_note or ""
            result.cuts.append(Cut(s.name, "dropped", b, estimate_tokens(s.text)))
    if over() > 0:                                    # 3. clip what must stay
        for s in sorted((s for s in work if s.required and s.text), key=lambda s: -len(s.text)):
            if over() <= 0:
                break
            b = estimate_tokens(s.text)
            target = max(COMPRESS_FLOOR_CHARS, len(s.text) - tokens_to_chars(over()) - 200)
            s.text = clip(s.text, target)
            result.cuts.append(Cut(s.name, "clipped", b, estimate_tokens(s.text)))
    result.tokens_after = total_tokens(work)
    return result


# --- the context-updates block: exactly one per prompt ----------------------------------------

def updates_markers() -> Tuple[str, str]:
    """The context-updates block's delimiters, from the module that owns them."""
    try:
        from ..runner.context_updates import BLOCK_END, BLOCK_START
        return BLOCK_START, BLOCK_END
    except Exception:  # noqa: BLE001 - the strings are stable; never fail a prompt over an import
        return "=== CONTEXT UPDATES ===", "=== END CONTEXT UPDATES ==="


SUPERSEDED_UPDATES_NOTE = ("[An earlier context-updates block stood here. It was removed because "
                           "it is superseded by the current one in this prompt.]")
STALE_UPDATES_NOTE = ("[A context-updates block quoted from an earlier run stood here. It was "
                      "removed: it is not this run's material.]")


def count_blocks(text: Optional[str]) -> int:
    """How many context-updates blocks ``text`` holds."""
    start, _end = updates_markers()
    return (text or "").count(start)


def block_spans(text: str) -> List[Tuple[int, int]]:
    """Every context-updates block in ``text``, gate included (``runner.context_updates`` owns
    the format, so it owns the parsing; this only delegates)."""
    try:
        from ..runner.context_updates import block_spans as owner_spans
    except Exception:  # noqa: BLE001 - never fail a prompt over an import
        return []
    return owner_spans(text or "")


def keep_last_block(text: Optional[str], *, note: str = SUPERSEDED_UPDATES_NOTE) -> str:
    """``text`` with every context-updates block but the LAST replaced by ``note``.

    The last one is the newest: a composed brief puts its own block after anything it quotes, and
    the deep prompt puts the task after its preamble. Text with one block or none is unchanged.
    """
    body = text or ""
    spans = block_spans(body)
    if len(spans) <= 1:
        return body
    out: List[str] = []
    pos = 0
    for a, b in spans[:-1]:
        out.append(body[pos:a])
        out.append(note)
        pos = b
    out.append(body[pos:])
    return "".join(out)


def strip_blocks(text: Optional[str], *, note: str = STALE_UPDATES_NOTE) -> str:
    """``text`` with EVERY context-updates block replaced by ``note`` (for quoted material)."""
    body = text or ""
    spans = block_spans(body)
    if not spans:
        return body
    out: List[str] = []
    pos = 0
    for a, b in spans:
        out.append(body[pos:a])
        out.append(note)
        pos = b
    out.append(body[pos:])
    return "".join(out)


# --- the budget and the model window ----------------------------------------------------------

def context_window_tokens(model: Optional[str]) -> int:
    """The context window assumed for ``model`` (a ``[1m]`` id gets the long window)."""
    m = (model or "").strip().lower()
    if m.endswith("[1m]") or "-1m" in m:
        return LONG_CONTEXT_WINDOW_TOKENS
    return DEFAULT_CONTEXT_WINDOW_TOKENS


def max_prompt_tokens_for(model: Optional[str]) -> int:
    """The most our prompt may use for ``model``: its window minus the worker's reserve."""
    return max(MIN_DEEP_PROMPT_TOKEN_BUDGET,
               context_window_tokens(model) - WORKER_RESERVED_TOKENS)


def resolve_deep_prompt_budget(configured: Optional[int] = None, *,
                               model: Optional[str] = None) -> int:
    """The token budget for one deep prompt.

    ``configured`` (a consumer's explicit value) wins, else ``QAR_DEEP_PROMPT_TOKEN_BUDGET``, else
    ``DEFAULT_DEEP_PROMPT_TOKEN_BUDGET``; whichever it is, it is capped at what ``model``'s window
    can hold (``max_prompt_tokens_for``), so no setting can ask for a prompt the model must refuse.
    """
    budget: Optional[int] = configured if configured and configured > 0 else None
    if budget is None:
        raw = (os.environ.get(DEEP_PROMPT_BUDGET_ENV) or "").strip()
        if raw:
            try:
                budget = int(raw)
            except ValueError:
                log.warning("%s=%r is not an integer; using the default %d", DEEP_PROMPT_BUDGET_ENV,
                            raw, DEFAULT_DEEP_PROMPT_TOKEN_BUDGET)
    if not budget or budget <= 0:
        budget = DEFAULT_DEEP_PROMPT_TOKEN_BUDGET
    return max(MIN_DEEP_PROMPT_TOKEN_BUDGET, min(budget, max_prompt_tokens_for(model)))


TOO_LONG_RE = re.compile(r"prompt is too long|context(?: window)? (?:length )?exceeded|"
                          r"input is too long|too many (?:input )?tokens", re.I)


def is_prompt_too_long(text: Optional[str]) -> bool:
    """Whether a worker's error text says its prompt did not fit the model's window."""
    return bool(TOO_LONG_RE.search(text or ""))


# --- the deep worker's prompt -----------------------------------------------------------------

# Block headers this library emits into a deep preamble, mapped to how much each is worth. A header
# is the whole line. Anything before the first header is the lane's own standing preamble.
HEADER_RE = re.compile(r"^(?:=== (?!END\b)[^\n]{2,80} ===|--- [A-Z][^\n]{2,80} ---|"
                        r"## (?:Keyword|Vector) context[^\n]*)$", re.M)

HEADER_PRIORITY: List[Tuple[str, int]] = [
    ("CONTEXT DOCTRINE", PRIORITY_STANDING),
    ("ACT AS THIS PERSON", PRIORITY_STANDING),
    ("LEARNED CORRECTIONS", PRIORITY_STANDING),
    ("CONTEXT UPDATES", PRIORITY_UPDATES),
    ("RELEVANT CONTENT FOUND BY THE BRAIN", PRIORITY_PLAN),
    ("CONTEXT SELECTED FOR THIS GOAL", PRIORITY_PLAN),
    ("RELEVANT CONVERSATION FOR THIS GOAL", PRIORITY_HISTORY),
    ("PRIOR CONVERSATION CONTEXT", PRIORITY_HISTORY),
    ("RELEVANT PAST CONVERSATIONS", PRIORITY_HISTORY),
    ("RELEVANT PAST CLAUDE SESSIONS", PRIORITY_HISTORY),
    ("CONTEXT FROM RECENT TURNS", PRIORITY_RETRIEVAL),
    ("AI REP CONTEXT", PRIORITY_RETRIEVAL),
    ("KEYWORD CONTEXT", PRIORITY_RETRIEVAL),
    ("VECTOR CONTEXT", PRIORITY_RETRIEVAL),
]


def priority_for_header(header: str) -> int:
    h = header.upper()
    for key, prio in HEADER_PRIORITY:
        if key in h:
            return prio
    return PRIORITY_HISTORY


def split_preamble(preamble: Optional[str]) -> List[Section]:
    """A deep preamble as sections, one per block header this library emits."""
    text = (preamble or "").strip()
    if not text:
        return []
    marks = [m for m in HEADER_RE.finditer(text)]
    sections: List[Section] = []
    first = marks[0].start() if marks else len(text)
    head = text[:first].strip()
    if head:
        sections.append(Section("standing preamble", head, PRIORITY_STANDING))
    for i, m in enumerate(marks):
        stop = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        chunk = text[m.start():stop].strip()
        if not chunk:
            continue
        header = m.group(0).strip("=-# ").strip()
        prio = priority_for_header(header)
        sections.append(Section(header.lower(), chunk, prio,
                                required=(prio == PRIORITY_STANDING and "DOCTRINE" in header.upper()),
                                drop_note=f"[{header}: left out to fit this run's prompt budget]"))
    return sections


def fit_deep_prompt(preamble: str, task_parts: List[str], *, budget_tokens: int,
                    label: str = "deep prompt") -> Tuple[str, List[str], FitResult]:
    """Fit a deep worker's preamble and task parts into ``budget_tokens``.

    ``task_parts`` are the fixed parts of the prompt that follow the preamble (the TASK, the GOAL,
    the contracts); they are required. The preamble is split into its blocks and cut by priority.
    Exactly one context-updates block survives across the whole prompt (the last).

    Returns ``(preamble, task_parts, fit)``, each already fitted. Logs one INFO line naming every
    cut when anything was cut.
    """
    start, _end = updates_markers()
    pre = preamble or ""
    parts = list(task_parts)
    # One block per prompt. The task parts come last, so a block there is the live one: every
    # block in the preamble goes, and inside the task text only the last survives.
    if any(start in p for p in parts):
        pre = strip_blocks(pre, note=SUPERSEDED_UPDATES_NOTE) if start in pre else pre
        joined_has = [i for i, p in enumerate(parts) if start in p]
        last = joined_has[-1]
        for i in joined_has[:-1]:
            parts[i] = strip_blocks(parts[i], note=SUPERSEDED_UPDATES_NOTE)
        parts[last] = keep_last_block(parts[last])
    elif start in pre:
        pre = keep_last_block(pre)

    sections = split_preamble(pre)
    n_pre = len(sections)
    sections += [Section(f"task part {i + 1}", p, PRIORITY_REQUEST, required=True)
                 for i, p in enumerate(parts)]
    fit = fit_sections(sections, budget_tokens)
    if not any(c.name in {s.name for s in sections[:n_pre]} for c in fit.cuts):
        # Nothing in the preamble was cut: hand it back exactly as it came, so a prompt under
        # budget is byte-identical to the unbudgeted composition.
        fitted_pre = pre
    else:
        fitted_pre = "\n\n".join(s.text for s in fit.sections[:n_pre] if s.text)
    fitted_parts = [s.text for s in fit.sections[n_pre:]]
    if fit.cuts:
        log.info("%s fitted to its budget: %s", label, fit.summary())
    return fitted_pre, fitted_parts, fit


def describe_cuts(cuts: List[Cut], limit: int = 6) -> str:
    """A short human sentence about what was left out, for a progress line."""
    if not cuts:
        return ""
    names: Dict[str, None] = {}
    for c in cuts:
        names.setdefault(c.name, None)
    shown = list(names)[:limit]
    more = len(names) - len(shown)
    return ", ".join(shown) + (f" and {more} more" if more > 0 else "")


# What a small judgment call (is this card relevant, which files matter, what to keep) is given of
# the request. Such a call needs the gist, not the whole brief: a composed autopilot brief runs to
# 50K characters and more, and handing all of it to every judge cost a full brief per call while
# telling it nothing the opening and the closing request do not.
DECISION_EXCERPT_HEAD_CHARS = 2500
DECISION_EXCERPT_TAIL_CHARS = 1500


def decision_excerpt(text: Optional[str], *, head: int = DECISION_EXCERPT_HEAD_CHARS,
                     tail: int = DECISION_EXCERPT_TAIL_CHARS) -> str:
    """The request as a judge needs it: its opening and its end, the middle marked as cut.

    The head carries who is asking and the standing instructions; the tail carries what opened
    this particular run (a reply, today's scheduled turn). Context-updates blocks are left out
    first, since a judge is not the run that accounts for them. Short text comes back unchanged.
    """
    body = (text or "").strip()
    if len(body) <= head + tail + 200:
        return body
    body = strip_blocks(body, note="[context updates left out]")
    if len(body) <= head + tail + 200:
        return body
    return (body[:head].rstrip() + f"\n[... {len(body) - head - tail} characters of the request "
            f"left out for this judgment ...]\n" + body[-tail:].lstrip())

