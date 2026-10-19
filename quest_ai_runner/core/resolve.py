"""Selection as data: turn ``(solution_id, name)`` into the implementation, from ``selection_table.SELECTION``.

    cls = resolve("call-a-model", "gemini")                    # an explicit name
    cls = resolve("run-deep-work", overrides=cfg.selections)   # a per-solution override dict, else the default
    cls = resolve("search-the-web")                            # the table default

Precedence: explicit ``name``, then ``overrides[solution_id]``, then the project default. An unknown
solution or name raises ``ValueError`` listing the choices, so a typo in config fails loudly instead of
falling through an ``else``. Targets are imported lazily, so ``core`` never imports an adapter at import
time and an optional dependency is only needed when its implementation is chosen.

This module reads no environment: the caller decides the name (from config, env at the config layer, a
test) and passes it in. ``register`` adds an implementation at run time (a consumer's own backend), so a new
implementation is a data change, not an edit to a call site.
"""
from __future__ import annotations

import importlib
import json
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from .selection_table import SELECTION

_EXTRA: Dict[str, Dict[str, Any]] = {}   # run-time additions: {solution_id: {name: entry}}
_EXTRA_DEFAULT: Dict[str, str] = {}

# The project default is the solution's ``selected`` in esoi/solutions.csv, written once. QAR is installed without
# its records (quest-backend installs it), and esoi is a dev dependency, so the defaults ship as the snapshot
# ``esoi select --snapshot quest_ai_runner/esoi_selection.json`` writes: esoi's own format, read here with the
# standard library. tests/test_resolve.py fails when the snapshot differs from the records.
SNAPSHOT_PATH = Path(__file__).resolve().parent.parent / "esoi_selection.json"
_DEFAULTS: Dict[str, str] = {}


def project_defaults() -> Dict[str, str]:
    """{solution_id: implementation name} from the shipped snapshot (read once)."""
    if not _DEFAULTS:
        found = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
        _DEFAULTS.update({sid: entry["default"] for sid, entry in found.items() if entry.get("default")})
    return _DEFAULTS


def _implementations(solution_id: str) -> Dict[str, Any]:
    if solution_id not in SELECTION:
        raise ValueError(f"no selection table entry for solution {solution_id!r}; known: {sorted(SELECTION)}")
    return {**SELECTION[solution_id]["implementations"], **_EXTRA.get(solution_id, {})}


def default_name(solution_id: str) -> str:
    """The implementation used when nothing asks for another."""
    _implementations(solution_id)
    found = _EXTRA_DEFAULT.get(solution_id) or project_defaults().get(solution_id)
    if not found:
        raise ValueError(f"solution {solution_id!r} has no default: the snapshot {SNAPSHOT_PATH.name} names none "
                         "(run `esoi select --snapshot quest_ai_runner/esoi_selection.json`)")
    return found


def names(solution_id: str) -> List[str]:
    """Every selectable implementation name for a solution, sorted."""
    return sorted(_implementations(solution_id))


def chosen_name(solution_id: str, name: Optional[str] = None, overrides: Optional[Mapping[str, str]] = None) -> str:
    """The implementation name that ``resolve`` would use (explicit, then override, then default)."""
    picked = (name or (overrides or {}).get(solution_id) or default_name(solution_id))
    picked = str(picked).strip().lower()
    if picked not in _implementations(solution_id):
        raise ValueError(f"unknown implementation {picked!r} for {solution_id}; choices: {', '.join(names(solution_id))}")
    return picked


def _load(ref: str) -> Any:
    module, _, attr = ref.partition(":")
    return getattr(importlib.import_module(module), attr)


def resolve(solution_id: str, name: Optional[str] = None, *, overrides: Optional[Mapping[str, str]] = None) -> Any:
    """The class or factory for the chosen implementation of ``solution_id`` (not an instance)."""
    entry = _implementations(solution_id)[chosen_name(solution_id, name, overrides)]
    return _load(entry["target"] if isinstance(entry, dict) else entry)


def resolve_config(solution_id: str, name: Optional[str] = None, *, overrides: Optional[Mapping[str, str]] = None) -> Any:
    """The config dataclass the chosen implementation is built from, or ``None`` when it takes none."""
    entry = _implementations(solution_id)[chosen_name(solution_id, name, overrides)]
    ref = entry.get("config") if isinstance(entry, dict) else None
    return _load(ref) if ref else None


def register(solution_id: str, name: str, target: str, *, config: Optional[str] = None, default: bool = False) -> None:
    """Add an implementation (``"pkg.module:Attr"``) to a known solution; ``default=True`` makes it the default."""
    _implementations(solution_id)
    key = name.strip().lower()
    _EXTRA.setdefault(solution_id, {})[key] = {"target": target, "config": config} if config else target
    if default:
        _EXTRA_DEFAULT[solution_id] = key
