"""The approach a worker takes for a run, chosen among distinct ways by the solution's requirements (the hub's ``choose_approach``), as a brief block and as
implementation records the chosen way and the ways not taken can be kept as. Fast tier calls only; the caller decides whether to write the records."""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple


def approach_block(frame: str, requirements: Sequence[str], decide: Callable[[str], str],
                   solution_id: Optional[str] = None) -> Tuple[str, List[Dict[str, Any]]]:
    """``(text, records)``: the block for a worker brief ("" when no approach came back) and the implementation records, each ``{name, description,
    disadvantages, selected}`` (plus ``solution_id`` when given), ready for ``spec_edit.apply`` as ``record_add`` on ``implementations``."""
    from hubofhubs.api import choose_approach
    got = choose_approach(frame, requirements, decide)
    chosen = got.get("chosen")
    if not chosen:
        return "", []
    others = list(got.get("alternatives") or [])
    text = f"Approach chosen for this run: {chosen['name']}: {chosen['how']}"
    if chosen.get("disadvantage"):
        text += f" Its known weakness: {chosen['disadvantage'].rstrip('.')}."
    if others:
        text += " Considered and not taken: " + "; ".join(f"{o['name']} ({o['disadvantage'].rstrip('.') or 'no weakness stated'})" for o in others) + "."

    def record(approach: Mapping[str, str], selected: bool) -> Dict[str, Any]:
        row: Dict[str, Any] = {"name": approach["name"], "description": approach["how"], "disadvantages": approach.get("disadvantage", ""), "selected": selected}
        if solution_id:
            row["solution_id"] = solution_id
        return row
    return text, [record(chosen, True)] + [record(o, False) for o in others]
