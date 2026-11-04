"""Where the hub's evaluation folder is found. The topic cases themselves are gone from this metric: the hub's ``context_retrieval`` metric
owns the topic kind (and the file, orientation and no-context kinds), and ``metric-select-context`` builds on it (``foundation_adapter.py``,
``esoi.toml`` ``[builds_on.context_selection.retrieval]``). Only the helper that puts the hub's evaluation folder on ``sys.path`` stays,
because ``hub_seat.py`` reads the hub's co-change history through it."""
import sys
from pathlib import Path


def hub_helpers():
    """The hub's history reader and subject filter, and the commit its pin names, found beside the hubofhubs package (its evaluation folder)."""
    import hubofhubs
    evaluation = Path(hubofhubs.__file__).resolve().parents[1] / "evaluation"
    if str(evaluation) not in sys.path:
        sys.path.insert(0, str(evaluation))
    from cards_from_files import corpus as pinned
    from cards_from_files import topic_cases as hub_cases
    return hub_cases.history_of, hub_cases.names_something, pinned.read_pin()["qar_commit"]
