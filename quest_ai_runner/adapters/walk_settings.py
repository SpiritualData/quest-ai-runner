"""QAR's setting for the hub's file walks: the directories a deployment never searches.

The walk helpers (``hubofhubs.recall.walk``) read no environment. This module reads ``QAR_SKIP_DIRS`` (comma-separated bare
directory names: backups, archives, data dumps) and always adds QAR's own card store folder, then hands the names to the hub's
``effective_skip_dirs`` as ``extra``. Every QAR walk asks here for its skip set.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Set

from hubofhubs.recall.walk import effective_skip_dirs as hub_skip_dirs

STORE_DIR_NAME = ".quest-context"


def skip_dirs_from_env() -> Set[str]:
    """The names in ``QAR_SKIP_DIRS`` plus the card store folder."""
    named = {n.strip() for n in os.getenv("QAR_SKIP_DIRS", "").split(",") if n.strip()}
    return named | {STORE_DIR_NAME}


def effective_skip_dirs(root: Path) -> Set[str]:
    """The hub's baseline skip set for ``root`` plus QAR's directories (the same set the walks always used)."""
    return hub_skip_dirs(root, extra=skip_dirs_from_env())


def conversation_scan_skip_dirs(corpus_root) -> "Set[str] | None":
    """The skip set for a scan of ``corpus_root`` for conversation folders, or None when there is no corpus to scan."""
    return effective_skip_dirs(Path(corpus_root)) if corpus_root else None
