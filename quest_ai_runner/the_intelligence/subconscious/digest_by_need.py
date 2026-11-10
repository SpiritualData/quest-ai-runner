"""``digest-records-for-a-need:digest_by_need``: a need reads a large body of the person's records one group at a time, as sub-analyses, and receives cited findings.

A single prompt holding every entry of a big collection is neither affordable nor readable. This arm lets the need decide what is read and to what
depth. The need (a question, plus the background it is asked in) goes to one cheap-tier sub-analysis per window of ONE source group; each returns the
findings that bear on THIS need with the ids of the records they rest on, or says nothing bears. Only ids that were in the window survive (checked in
code, never trusted from the model). The reduce step is plain code: findings are merged in a stable order (group id) and clipped to a character cap, so
the text handed on is a few cited lines, never the records. A model pass over the findings would add a call per run and could drop a citation.

Guards, all in code: windows and records are capped in size; a concurrency cap and a budget of sub-analyses per call (a window beyond the budget is
reported ``skipped``); a digest is cached under (need, background, content of the window), so an unchanged window is never read again and a different
need makes a different digest from the same records; a group that cannot be read or answered is reported ``failed`` with its reason, never dropped and
never failing the call; a body small enough to read whole (``direct_chars`` and ``direct_records``) is handed on as it is, with status ``direct``.

The call (``digest_need``) takes the need as ``task_text`` and everything specific in ``meta``; the selected arm answers, so the call names no arm:

    quest_ai_runner.run("digest-records-for-a-need", {"task_text": need, "meta": {"source_groups": [SourceGroup(...)], "background": outcome}})

``meta`` keys: ``source_groups`` (required), ``background``, ``need_key`` (a stable name for the need; default a hash of its text), ``cache`` (a
``DigestCache``; default one that lives as long as the arm), and any cap below by name. The answer is the hub's ``AssembledContext``: ``context_view`` is
the reduced text, ``card_ids`` the cited record ids, ``card_metadata`` their titles, ``sources`` the coverage (one entry labelled ``coverage`` with the
calls made and windows reused, then one entry per group with its status and reason), and ``partial`` is true when any group was not fully read.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from esoi import implementation
from hubofhubs.api import AssembledContext, ModelCalling, STEP_SELECT, answer_with_reasoning
from hubofhubs.api import card_selection

from quest_ai_runner.the_intelligence.subconscious.digest_prompts import records_block
from quest_ai_runner.the_self.integrity.prompts.prompt_layers import PromptLayers
from quest_ai_runner.the_self.integrity.prompts.prompt_specs import NO_EM_DASH_RULE, US_SPELLING_RULE, render_prompt
from quest_ai_runner.the_intelligence.subconscious.digest_sources import (
    STATUS_ANALYSED, STATUS_CACHED, STATUS_DIRECT, STATUS_FAILED, STATUS_NOTHING, STATUS_SKIPPED, UNREAD_STATUSES, DigestCache, DigestResult, GroupResult,
    MemoryDigestCache, SourceGroup, Unit)

log = logging.getLogger(__name__)

#: The defaults of every cap; a call overrides one by name in ``meta``.
DEFAULTS: Dict[str, int] = {
    "unit_chars": 600,            # one record is clipped to this many characters before it is read
    "window_chars": 6000,         # one sub-analysis reads at most this many characters of one group
    "max_units_per_group": 300,   # records of one group that are read at all
    "max_analyses": 16,           # sub-analyses (model calls) one call may spend; later windows are reported skipped
    "concurrency": 4,             # sub-analyses running at once
    "max_findings": 6,            # findings kept from one window
    "finding_chars": 300,
    "quote_chars": 200,
    "digest_chars": 9000,         # the reduced text is clipped to this
    "direct_chars": 3000,         # a body this small is handed on as it is (0 turns that off)
    "direct_records": 10,         # ...and so is a group of at most this many records
}
#: Changes when the prompts change, so a cached digest never outlives the wording that made it.
DIGEST_VERSION = "1"


def clip(text: Any, limit: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: max(0, limit - 3)].rstrip() + "..."


def plain(text: Any) -> str:
    """Model text with whitespace collapsed and no em dashes (a comma instead)."""
    return " ".join(re.sub(r"\s*\u2014\s*", ", ", str(text or "")).replace("\u2013", "-").split())


def windows_of(units: Sequence[Unit], caps: Mapping[str, int]) -> List[List[Unit]]:
    """The units cut, in order, into windows of at most ``window_chars`` characters, each unit clipped to ``unit_chars``."""
    windows: List[List[Unit]] = []
    current: List[Unit] = []
    used = 0
    for unit in list(units)[: caps["max_units_per_group"]]:
        text = clip(unit.text, caps["unit_chars"])
        if current and used + len(text) > caps["window_chars"]:
            windows.append(current)
            current, used = [], 0
        current.append(Unit(unit.source_id, unit.title, text))
        used += len(text)
    return windows + ([current] if current else [])


def window_hash(window: Sequence[Unit]) -> str:
    return hashlib.sha1("|".join(f"{unit.source_id}:{unit.text}" for unit in window).encode()).hexdigest()


def clean_findings(raw: Any, valid: set, caps: Mapping[str, int]) -> List[Dict[str, Any]]:
    """The model's findings with only ids that were in the window (an invented id is dropped; a finding left with none is dropped), clipped."""
    out: List[Dict[str, Any]] = []
    for row in (raw.get("findings") if isinstance(raw, dict) else None) or []:
        if not isinstance(row, dict):
            continue
        cited = [source_id for source_id in dict.fromkeys(str(x).strip() for x in row.get("source_ids") or []) if source_id in valid]
        text = clip(plain(row.get("text")), caps["finding_chars"])
        if text and cited:
            out.append({"text": text, "source_ids": cited, "quote": clip(plain(row.get("quote")), caps["quote_chars"])})
    return out[: caps["max_findings"]]


class Window:
    """One window waiting to be read: where it came from, its cache key, and what the read produced."""

    def __init__(self, group: SourceGroup, units: List[Unit], key: str) -> None:
        self.group, self.units, self.key = group, units, key
        self.status, self.reason, self.findings = "", "", []   # type: str, str, List[Dict[str, Any]]


class Digester:
    """Reads source groups for one need. One instance serves one call: the budget and the counters are the call's."""

    def __init__(self, provider: Optional[ModelCalling], model: Optional[str], cache: DigestCache, caps: Mapping[str, int]) -> None:
        self.provider, self.model, self.cache, self.caps = provider, model, cache, caps
        self.analyses = 0
        self.reused = 0

    def read_window(self, need: str, background: str, window: Window) -> None:
        head = render_prompt("digest-records-for-a-need.digest-head", need=need, background=background or "(none given)", finding_chars=self.caps["finding_chars"],
                             max_findings=self.caps["max_findings"], no_em_dash=f"{NO_EM_DASH_RULE} {US_SPELLING_RULE}")
        tail = render_prompt("digest-records-for-a-need.digest-group", group_title=window.group.title, records=records_block([(u.source_id, u.text) for u in window.units]))
        prompt = PromptLayers(head=head, tail=tail)
        try:
            raw = answer_with_reasoning(self.provider, [{"role": "user", "content": prompt.render()}], model=self.model, reasoning="minimal",
                                        step=STEP_SELECT, layers=prompt.blocks())
            parsed = json.loads(card_selection.extract_json(raw or "") or "null")
        except Exception as error:  # noqa: BLE001 - recorded on the group, never fails the call
            log.warning("digest_by_need: reading %s failed: %s", window.group.group_id, error)
            window.status, window.reason = STATUS_FAILED, clip(plain(error), 200) or type(error).__name__
            return
        if not isinstance(parsed, dict):
            window.status, window.reason = STATUS_FAILED, "the reply could not be read"
            return
        window.findings = clean_findings(parsed, {unit.source_id for unit in window.units}, self.caps)
        window.reason = "" if window.findings else clip(plain(parsed.get("reason")), 200) or "nothing in this group bears on the need"
        window.status = STATUS_ANALYSED if window.findings else STATUS_NOTHING

    def digest(self, need: str, background: str, need_key: str, groups: Sequence[SourceGroup]) -> DigestResult:
        prefix = hashlib.sha1(f"{DIGEST_VERSION}|{need_key}|{background}".encode()).hexdigest()
        loaded: List[Tuple[SourceGroup, List[Unit], Optional[GroupResult], List[Window]]] = []
        budget = self.caps["max_analyses"]
        to_read: List[Window] = []
        for group in groups:
            try:
                units = list(group.units or (group.loader() if group.loader else []))
            except Exception as error:  # noqa: BLE001 - recorded on the group, never fails the call
                log.warning("digest_by_need: loading %s failed: %s", group.group_id, error)
                loaded.append((group, [], GroupResult(group.group_id, group.title, STATUS_FAILED, f"could not be read: {type(error).__name__}"), []))
                continue
            windows = [Window(group, w, hashlib.sha1(f"{prefix}|{window_hash(w)}".encode()).hexdigest()) for w in windows_of(units, self.caps)]
            for window in windows:
                stored = self.cache.get(window.key)
                if stored is not None:
                    self.reused += 1
                    window.status, window.reason, window.findings = STATUS_CACHED, stored.get("reason", ""), list(stored.get("findings") or [])
                elif self.provider is None:
                    window.status, window.reason = STATUS_SKIPPED, "no model is available to read this group"
                elif budget <= 0:
                    window.status, window.reason = STATUS_SKIPPED, "over the budget of sub-analyses for one call"
                else:
                    budget -= 1
                    to_read.append(window)
            loaded.append((group, units, None, windows))
        self.analyses = len(to_read)
        with ThreadPoolExecutor(max_workers=max(1, self.caps["concurrency"])) as pool:
            list(pool.map(lambda window: self.read_window(need, background, window), to_read))
        for window in to_read:
            if window.status in (STATUS_ANALYSED, STATUS_NOTHING):
                self.cache.put(window.key, {"findings": window.findings, "reason": window.reason})
        results = [failed or group_result(group, units, windows) for group, units, failed, windows in loaded]
        return DigestResult(reduce_text(results, self.caps["digest_chars"]), results, self.analyses, self.reused)


def group_result(group: SourceGroup, units: Sequence[Unit], windows: Sequence[Window]) -> GroupResult:
    statuses = [window.status for window in windows]
    findings = [f for window in windows for f in window.findings]
    read = sum(len(window.units) for window in windows if window.status in (STATUS_ANALYSED, STATUS_CACHED, STATUS_NOTHING))
    titles = {unit.source_id: unit.title for window in windows for unit in window.units}
    for bad in UNREAD_STATUSES:
        if bad in statuses:
            return GroupResult(group.group_id, group.title, bad, next(w.reason for w in windows if w.status == bad), findings, read, len(units), titles)
    if not windows:
        return GroupResult(group.group_id, group.title, STATUS_NOTHING, "the group has no records", [], 0, 0)
    status = STATUS_CACHED if all(s == STATUS_CACHED for s in statuses) else STATUS_ANALYSED if findings else STATUS_NOTHING
    reason = "" if findings else next((w.reason for w in windows if w.reason), "")
    return GroupResult(group.group_id, group.title, status, reason, findings, read, len(units), titles)


def reduce_text(results: Sequence[GroupResult], cap: int) -> str:
    """The reduce step, in code: every group's findings in a stable order (group id), each citing its record ids, within ``cap`` characters."""
    blocks = []
    for result in sorted(results, key=lambda r: r.group_id):
        if not result.findings:
            continue
        lines = [f"- {f['text']} [sources: {', '.join(f['source_ids'])}]" + (f' (quote: "{f["quote"]}")' if f.get("quote") else "") for f in result.findings]
        blocks.append(f"{result.title}\n" + "\n".join(lines))
    return clip_blocks("\n\n".join(blocks), cap)


def clip_blocks(text: str, cap: int) -> str:
    """``text`` cut at ``cap`` characters on a line boundary, keeping its line breaks."""
    if len(text) <= cap:
        return text
    cut = text[:cap]
    return cut[: cut.rfind("\n")] if "\n" in cut else cut


def direct_result(groups: Sequence[SourceGroup], units: Mapping[str, List[Unit]]) -> DigestResult:
    """A body small enough to read whole: its records as they are, under their ids, with status ``direct``."""
    results, blocks = [], []
    for group in sorted(groups, key=lambda g: g.group_id):
        rows = units[group.group_id]
        results.append(GroupResult(group.group_id, group.title, STATUS_DIRECT, "", [], len(rows), len(rows), {u.source_id: u.title for u in rows}))
        if rows:
            blocks.append(f"{group.title}\n" + "\n".join(f"[{u.source_id}] {u.text}" for u in rows))
    return DigestResult("\n\n".join(blocks), results)


def caps_from(meta: Mapping[str, Any], base: Mapping[str, int]) -> Dict[str, int]:
    caps = dict(base)
    for name in DEFAULTS:
        if isinstance(meta.get(name), int) and not isinstance(meta.get(name), bool):
            caps[name] = meta[name]
    return caps


def assembled(result: DigestResult) -> AssembledContext:
    """The hub's context value for a digest: the text, the cited ids with their titles, and the coverage of every group."""
    cited = list(dict.fromkeys(i for r in result.groups for f in r.findings for i in f["source_ids"]))
    if any(r.status == STATUS_DIRECT for r in result.groups):
        cited = [i for r in sorted(result.groups, key=lambda r: r.group_id) for i in r.titles]
    titles = {i: t for r in result.groups for i, t in r.titles.items()}
    owner = {i: r.group_id for r in result.groups for i in r.titles}
    sources = [{"adapter": "digest_by_need", "label": "coverage", "items": [], "analyses": result.analyses, "reused": result.reused}]
    sources += [{"adapter": "digest_by_need", "label": r.title, "items": [i for f in r.findings for i in f["source_ids"]], **r.public()} for r in result.groups]
    return AssembledContext(context_view=result.text, card_ids=cited, sources=sources,
                            card_metadata=[{"id": i, "title": titles.get(i, ""), "group": owner.get(i, ""), "adapter": "digest_by_need"} for i in cited],
                            partial=any(r.status in UNREAD_STATUSES for r in result.groups))


@implementation("digest-records-for-a-need", "digest_by_need", scope="quest_ai_runner")
class DigestByNeed:
    """Reads a large body of records one source group at a time for one need and hands on cited findings (module docstring). The model is a
    constructor need (the ``vendor-model-caller`` seat); with none, every group that is not cached is reported skipped and nothing is invented."""
    REQUIRES = ()

    def __init__(self, provider: ModelCalling, model: Optional[str] = None, cache: Optional[DigestCache] = None) -> None:
        self.provider, self.model = provider, model
        self.cache: DigestCache = cache if cache is not None else MemoryDigestCache()

    def digest(self, need: str, groups: Sequence[SourceGroup], *, background: str = "", need_key: Optional[str] = None,
               cache: Optional[DigestCache] = None, **caps: int) -> DigestResult:
        """The typed form of ``choose``: the same read, returning every group's result and the calls made."""
        settings = caps_from(caps, DEFAULTS)
        groups = list(groups)
        if settings["direct_chars"] > 0:
            loaded: Dict[str, List[Unit]] = {}
            try:
                for group in groups:
                    loaded[group.group_id] = list(group.units or (group.loader() if group.loader else []))
            except Exception:  # noqa: BLE001 - a group that cannot be read is reported by the full path below
                loaded = {}
            if loaded and sum(len(u.text) for rows in loaded.values() for u in rows) <= settings["direct_chars"] \
                    and all(len(rows) <= settings["direct_records"] for rows in loaded.values()):
                return direct_result(groups, loaded)
            groups = [SourceGroup(g.group_id, g.title, loaded[g.group_id]) if g.group_id in loaded else g for g in groups]
        key = need_key or hashlib.sha1(need.encode()).hexdigest()[:16]
        return Digester(self.provider, self.model, cache if cache is not None else self.cache, settings).digest(clip(need, 1500), clip(background, 600), key, groups)

    def digest_need(self, task_text: str, *, meta: Optional[Dict[str, Any]] = None) -> AssembledContext:
        meta = dict(meta or {})
        groups = meta.get("source_groups")
        if not groups:
            return AssembledContext()
        result = self.digest(task_text, groups, background=str(meta.get("background") or ""), need_key=meta.get("need_key"), cache=meta.get("cache"),
                             **{name: meta[name] for name in DEFAULTS if name in meta})
        return assembled(result)
