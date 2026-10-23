"""quest_spec_sync: write a quest's ESOI specification into its mapped folder as ``ESOI_SPEC.md``.

The pull in :mod:`quest_folder_sync` calls :func:`pull_spec_to_folder` after it refreshes the sync
file. The file holds one managed block (``QAR:MANAGED:spec``) with a section per record, grouped by
type, each headed by a stable anchor ``### <T>: <name> (<recordId>)`` (``P`` problem, ``I`` ideal,
``S`` solution, ``IM`` implementation, ``M`` metric), so a persona prompt can say "ESOI_SPEC.md,
section <recordId>". Text a person adds outside the block is kept, through the same managed-block
helpers ``QUEST_SYNC.md`` uses.

Reading the spec is one GET and sends nothing. The server's ``digest`` is the hash of the inputs the
last REFRESH saw, so a person's edit to a record does not move it; it is therefore recorded in the
block header for a reader, while the decision not to write is made on the rendered content: a spec
that renders byte-identical leaves the file untouched (no mtime churn).
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from ._managed_sections import replace_between

log = logging.getLogger("quest-ai-runner.quest_spec_sync")

SPEC_START = "<!-- QAR:MANAGED:spec START -->"
SPEC_END = "<!-- QAR:MANAGED:spec END -->"

#: type -> (section heading, short anchor letter), in the order the file lists them.
TYPE_ORDER = (
    ("problem", "Problems", "P"),
    ("ideal", "Ideals", "I"),
    ("solution", "Solutions", "S"),
    ("implementation", "Implementations", "IM"),
    ("metric", "Metrics", "M"),
)

NAME_KEYS = ("name", "title")


def text_of(value: Any) -> str:
    """A field value as readable markdown text; empty when it holds nothing."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (list, tuple)):
        parts = [text_of(v) for v in value]
        parts = [p for p in parts if p]
        if all("\n" not in p for p in parts):
            return "\n".join(f"- {p}" for p in parts)
        return "\n".join(f"- {p.replace(chr(10), chr(10) + '  ')}" for p in parts)
    if isinstance(value, dict):
        parts = [f"{k}: {text_of(v)}" for k, v in value.items() if text_of(v)]
        return "\n".join(f"- {p}" for p in parts)
    return str(value).strip()


def record_name(record: Dict[str, Any]) -> str:
    fields = record.get("fields") or {}
    for key in NAME_KEYS:
        found = text_of(fields.get(key))
        if found:
            return " ".join(found.split())
    return str(record.get("recordId") or "(unnamed)")


def render_record(record: Dict[str, Any], letter: str) -> str:
    fields = record.get("fields") or {}
    record_id = str(record.get("recordId") or "")
    lines = [f"### {letter}: {record_name(record)} ({record_id})"]
    for key, value in fields.items():
        if key in NAME_KEYS:
            continue
        body = text_of(value)
        if not body:
            continue
        if "\n" in body:
            lines += ["", f"**{key}:**", body]
        else:
            lines += ["", f"**{key}:** {body}"]
    return "\n".join(lines)


def render_spec_block(spec: Dict[str, Any]) -> str:
    """The managed block body for one ``get_spec`` answer."""
    records = [r for r in (spec.get("records") or []) if isinstance(r, dict)]
    lines = ["# Specification (ESOI)", "",
             f"<!-- spec-digest: {spec.get('digest') or ''} -->",
             "Problems, ideals, solutions, implementations and metrics of this quest. Cite a record "
             "as `ESOI_SPEC.md, section <recordId>`."]
    known = {row[0] for row in TYPE_ORDER}
    for rtype, heading, letter in TYPE_ORDER:
        group = [r for r in records if r.get("type") == rtype]
        if not group:
            continue
        lines += ["", f"## {heading}"]
        for record in group:
            lines += ["", render_record(record, letter)]
    other = [r for r in records if r.get("type") not in known]
    if other:
        lines += ["", "## Other records"]
        for record in other:
            lines += ["", render_record(record, str(record.get("type") or "?")[:2].upper())]
    if not records:
        lines += ["", "_(no specification yet)_"]
    return "\n".join(lines)


def render_spec_file(existing: str, quest_id: str, spec: Dict[str, Any]) -> str:
    out = existing or ""
    if not re.match(r"^---\n.*?\n---\n?", out, re.DOTALL):
        out = f"---\nquest_id: {quest_id}\n---\n\n{out}"
    return replace_between(out, SPEC_START, SPEC_END, render_spec_block(spec))


def pull_spec_to_folder(client: Any, quest_id: str, folder: str, *,
                        filename: str = "ESOI_SPEC.md") -> Optional[int]:
    """Write the quest's spec into ``folder``/``filename`` when its content changed.

    Returns the number of records the file now holds, or None when the spec could not be read (the
    file is then left exactly as it was). Never raises: a spec problem must not stop a pull. A quest
    with no records and no existing file gets no file.
    """
    try:
        spec = client.get_spec("quest", quest_id)
    except Exception as e:  # noqa: BLE001 -- best-effort by contract
        log.info("spec read for quest %s failed (%s); %s left as it was", quest_id, e, filename)
        return None
    if not isinstance(spec, dict):
        return None
    records: List[Any] = spec.get("records") or []
    path = Path(folder) / filename
    try:
        existing = path.read_text(encoding="utf-8") if path.exists() else ""
        if not records and not existing:
            return 0
        rendered = render_spec_file(existing, quest_id, spec)
        if rendered != existing:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(rendered, encoding="utf-8")
    except OSError as e:  # pragma: no cover - filesystem edge
        log.warning("could not write %s for quest %s: %s", filename, quest_id, e)
        return None
    return len(records)
