"""Selection by name: turn ``(solution_id, name)`` into the implementation the code declares.

    cls = resolve("call-a-model", "gemini")                    # an explicit name
    cls = resolve("run-deep-work", overrides=cfg.selections)   # a per-solution override dict, else the default
    cls = resolve("search-the-web")                            # the project default

Every implementation is declared beside its code with ``@implementation("<solution id>", "<name>")`` (``esoi``), and
``esoi/implementations.csv`` lists the same implementations with the file of each, so there is no table to keep in step.
This module is the library's thin entry point over ``esoi.runtime`` under the scope ``quest_ai_runner``: a consumer that
also uses esoi for its own solutions (the same ids can appear in two projects) never meets ours.

Which implementation runs is one cascade, innermost first: the explicit ``name`` or ``overrides[solution_id]``, an
``esoi.override(...)`` block, ``ESOI_SELECT_QUEST_AI_RUNNER_<SOLUTION_ID>`` (or the plain ``ESOI_SELECT_<SOLUTION_ID>``),
``[selection]`` in ``esoi.toml``, and the project default: the solution's ``selected`` in ``esoi/solutions.csv``, written
once (an installed copy, which ships without those records, reads ``esoi_selection.json``, written by
``esoi select --snapshot quest_ai_runner/esoi_selection.json``). An unknown solution or name raises ``ValueError`` listing
the choices, so a typo in config fails loudly instead of falling through an ``else``. Implementations are imported lazily,
from the file their record names, so ``core`` never imports an adapter at import time and an optional dependency is only
needed when its implementation is chosen.

The only environment read here is esoi's own ``ESOI_SELECT_`` family; the other names (``QAR_MODEL_BACKEND`` and the
rest) are decided by the config layer and passed in. ``register`` adds an implementation of the consumer's own at run
time, so a new implementation is a data change, not an edit to a call site.
"""
from __future__ import annotations

import importlib
from typing import Any, Dict, List, Mapping, Optional, Tuple

from esoi import runtime

SCOPE = "quest_ai_runner"

CONFIG_REFS: Dict[Tuple[str, str], str] = {}   # run-time registrations that name a config class: (solution, name) -> "module:Attr"


def asked_name(solution_id: str, name: Optional[str], overrides: Optional[Mapping[str, str]]) -> Optional[str]:
    """The name the caller asks for: the explicit one, else the override for this solution, else none."""
    return name or (overrides or {}).get(solution_id) or None


def default_name(solution_id: str) -> str:
    """The implementation used when nothing asks for another."""
    return runtime.choose(solution_id, scope=SCOPE).name


def names(solution_id: str) -> List[str]:
    """Every selectable implementation name for a solution, sorted."""
    return runtime.names(solution_id, scope=SCOPE)


def chosen_name(solution_id: str, name: Optional[str] = None, overrides: Optional[Mapping[str, str]] = None) -> str:
    """The implementation name that ``resolve`` would use (explicit, then override, then default)."""
    return runtime.choose(solution_id, asked_name(solution_id, name, overrides), scope=SCOPE).name


def resolve(solution_id: str, name: Optional[str] = None, *, overrides: Optional[Mapping[str, str]] = None) -> Any:
    """The class or factory for the chosen implementation of ``solution_id`` (not an instance)."""
    return runtime.resolve(solution_id, asked_name(solution_id, name, overrides), scope=SCOPE)


def resolve_config(solution_id: str, name: Optional[str] = None, *, overrides: Optional[Mapping[str, str]] = None) -> Any:
    """The config dataclass the chosen implementation is built from, or ``None`` when it takes none."""
    picked = chosen_name(solution_id, name, overrides)
    ref = CONFIG_REFS.get((solution_id, picked))
    if ref:
        module, _, attr = ref.partition(":")
        return getattr(importlib.import_module(module), attr)
    return getattr(resolve(solution_id, picked), "config_class", None)


def register(solution_id: str, name: str, target: str, *, config: Optional[str] = None) -> None:
    """Add an implementation (``"pkg.module:Attr"``) of the consumer's own to a solution of this library, replacing the
    library's under the same name if there is one. To make it the default, name it in a ``selections`` entry,
    ``ESOI_SELECT_`` or ``[selection]`` in ``esoi.toml``."""
    runtime.register(solution_id, name, target, scope=SCOPE, replace=True)
    if config:
        CONFIG_REFS[(solution_id, name.strip().lower())] = config
