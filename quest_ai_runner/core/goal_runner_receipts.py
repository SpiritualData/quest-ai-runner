"""The deep worker's own receipts, read from its session record (the stream of tool uses and their results).

A run's receipts say what the worker DID, counted from the tool uses Claude Code recorded and whether each result was an error:
files written with their byte sizes, pages opened, web searches made, other tool calls. They are built from structure only
(tool names, error flags, input sizes), never from the worker's prose and never from the wording of any model output (hard rule 3),
and they carry no path, url or file name, so a reader sees counts and sizes, not machine details. They become
``DeepResult.observations`` so the interim policy (``core/interim_policy.py``) and the verifier have a run record to stand on.
"""
from __future__ import annotations

import json
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from hubofhubs.deep.session import FILE_CHANGE_TOOLS, resolve_session_file, session_record_paths

SEARCH_TOOLS = frozenset({"websearch"})
OPEN_TOOLS = frozenset({"webfetch"})
ID_PREFIX = "receipt:"   # marks a tool-use id in the "already there before this run" set a resumed run carries


def written_bytes(name: str, tool_input: Any) -> int:
    """Bytes the file-changing tool use put into a file, counted from its input (the new text, in UTF-8)."""
    if not isinstance(tool_input, dict):
        return 0
    texts: List[Any] = [tool_input.get("content"), tool_input.get("new_string"), tool_input.get("new_source")]
    for edit in tool_input.get("edits") or []:
        if isinstance(edit, dict):
            texts.append(edit.get("new_string"))
    return sum(len(t.encode("utf-8")) for t in texts if isinstance(t, str))


def tool_records(working_dir: Optional[str], session_id: Optional[str], config_dir: Optional[str] = None) -> Dict[str, Dict[str, Any]]:
    """Every tool use in the session (and its subagents), by id: ``{"name", "bytes", "error"}``. ``error`` is None until a result
    is seen, then True or False. Empty when the record cannot be found or read. Never raises."""
    path = resolve_session_file(working_dir, session_id, config_dir)
    records: Dict[str, Dict[str, Any]] = {}
    if path is None:
        return records
    for record_path in session_record_paths(path):
        try:
            with open(record_path, "r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if '"tool_use"' not in line and '"tool_result"' not in line:
                        continue
                    try:
                        content = (json.loads(line).get("message") or {}).get("content")
                    except (ValueError, AttributeError):
                        continue
                    for block in content if isinstance(content, list) else []:
                        if not isinstance(block, dict):
                            continue
                        if block.get("type") == "tool_use" and block.get("id"):
                            name = str(block.get("name") or "").lower()
                            records[str(block["id"])] = {"name": name, "error": None,
                                                         "bytes": written_bytes(name, block.get("input"))}
                        elif block.get("type") == "tool_result":
                            record = records.get(str(block.get("tool_use_id") or ""))
                            if record is not None:
                                record["error"] = block.get("is_error") is True
        except OSError:
            continue
    return records


def plural(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def kilobytes(n: int) -> str:
    return f"{n} bytes" if n < 1024 else f"{n / 1024:.1f} KB"


def receipt_lines(records: Dict[str, Dict[str, Any]], before: Iterable[str] = ()) -> List[str]:
    """Plain receipt lines for the tool uses not in ``before`` (ids of an earlier run of a resumed session), in a fixed order."""
    skip = {i[len(ID_PREFIX):] for i in before if str(i).startswith(ID_PREFIX)}
    mine = [r for i, r in records.items() if i not in skip]
    searches = [r for r in mine if r["name"] in SEARCH_TOOLS]
    opens = [r for r in mine if r["name"] in OPEN_TOOLS]
    writes = [r for r in mine if r["name"] in FILE_CHANGE_TOOLS and r["error"] is False]
    other = [r for r in mine if r["name"] not in SEARCH_TOOLS | OPEN_TOOLS | FILE_CHANGE_TOOLS]
    lines: List[str] = []
    if searches:
        ok = sum(1 for r in searches if r["error"] is False)
        lines.append(f"Ran {plural(len(searches), 'web search', 'web searches')}, {ok} returned results")
    if opens:
        ok = sum(1 for r in opens if r["error"] is False)
        lines.append(f"Tried to open {plural(len(opens), 'page', 'pages')}: {ok} loaded, {len(opens) - ok} could not be opened")
    if writes:
        lines.append(f"Wrote or changed {plural(len(writes), 'file', 'files')}, {kilobytes(sum(r['bytes'] for r in writes))} of new text")
    if other:
        lines.append(f"Made {plural(len(other), 'other tool call', 'other tool calls')} (reading files, running commands)")
    return lines


def receipt_ids(records: Dict[str, Dict[str, Any]]) -> Set[str]:
    """The tool-use ids of a session, tagged so they can ride in the set the runner already keeps for a resumed run."""
    return {ID_PREFIX + i for i in records}


def split_before(already: Iterable[str]) -> Tuple[Set[str], Set[str]]:
    """(tagged receipt ids, other ids) of a ``landed_in`` set."""
    tagged = {i for i in already if str(i).startswith(ID_PREFIX)}
    return tagged, set(already) - tagged
