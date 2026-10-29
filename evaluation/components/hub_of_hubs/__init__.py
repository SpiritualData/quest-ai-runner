"""QAR's hub_of_hubs component: a thin binding of QAR's code to the hubofhubs scorer.

The registry and the scorer live in the hub repository (``../ai-hub-of-hubs-structure``, package ``hubofhubs``), a required
dependency of QAR. It is imported the way the esoi library is: installed, or, failing that, from the sibling checkout beside
this repository, which this module puts on the path (pytest also does, through ``pythonpath`` in ``pyproject.toml``)."""
import sys
from pathlib import Path

try:
    import hubofhubs  # noqa: F401
except ImportError:
    sibling = Path(__file__).resolve().parents[3].parent / "ai-hub-of-hubs-structure"
    if (sibling / "hubofhubs").is_dir():
        sys.path.insert(0, str(sibling))
