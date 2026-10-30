"""Make ``hubofhubs`` importable in an environment that was installed before quest-ai-runner required it.

``hubofhubs`` is a required dependency, but an editable install does not gain a dependency added later, so a virtualenv built
earlier (a personal lane, a team runner, a shell environment) fails at import with ``No module named 'hubofhubs'``. In the
maintainers' corpus the hub sits beside this repository (``../ai-hub-of-hubs-structure``), the same checkout the tests put on
the path. ``ensure_hub`` appends that checkout when the package is not installed (an installed copy always wins) and
otherwise stops with the one command that fixes the environment. It reads no environment variable.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Optional

HUB_REQUIREMENT = "hubofhubs[locks] @ git+https://github.com/SpiritualData/ai-hub-of-hubs-structure.git@main"


def sibling_checkout(package_file: str) -> Optional[Path]:
    """The hub checkout next to this repository (``launch_code/ai-hub-of-hubs-structure``), or ``None`` when absent."""
    candidate = Path(package_file).resolve().parents[2] / "ai-hub-of-hubs-structure"
    return candidate if (candidate / "hubofhubs" / "__init__.py").is_file() else None


def ensure_hub(package_file: str = __file__) -> None:
    """Return when ``hubofhubs`` can be imported, adding the sibling checkout to the path if that is how; else raise."""
    if importlib.util.find_spec("hubofhubs") is not None:
        return
    found = sibling_checkout(package_file)
    if found is None:
        raise ModuleNotFoundError(
            "quest-ai-runner requires hubofhubs, which this environment does not have. Install it into the environment "
            f"that runs quest-ai-runner: pip install '{HUB_REQUIREMENT}'  (or, from a corpus checkout, pip install -e "
            "../ai-hub-of-hubs-structure)", name="hubofhubs")
    sys.path.append(str(found))   # appended, not inserted: an installed copy would already have been found above
