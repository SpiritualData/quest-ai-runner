"""QAR's hub_of_hubs component: a thin binding of QAR's code to the ai-hub-of-hubs-structure scorer.

The registry and the scorer live in the hub repository (``../ai-hub-of-hubs-structure``). It is imported the way the esoi library
is: installed (``pip install -e ../ai-hub-of-hubs-structure``, or the ``hubs`` extra) or, failing that, from the sibling checkout
beside this repository, which this module puts on the path."""
import sys
from pathlib import Path

try:
    import ai_hub_of_hubs  # noqa: F401
except ImportError:
    sibling = Path(__file__).resolve().parents[3].parent / "ai-hub-of-hubs-structure"
    if (sibling / "ai_hub_of_hubs").is_dir():
        sys.path.insert(0, str(sibling))
