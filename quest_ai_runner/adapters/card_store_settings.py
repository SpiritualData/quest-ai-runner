"""QAR's settings for the hub's card store: the environment variables a deployment sets, read here and handed over as arguments.

The hub's card store code (``hubofhubs.recall.card_*`` and ``context_*``) reads no environment. This module is the one place QAR
reads the variables that tune it, and it supplies QAR's names for the things the hub asks its consumer to name: the folder a
store keeps its cards in and the card limit's setting. Every function reads the environment when it is called, so a test
(or a deployment that sets the variable after import) sees the change.
"""
from __future__ import annotations

import os
from typing import Optional

from hubofhubs.recall.card_limit import parse_max_cards
from hubofhubs.recall.context_bootstrap_support import bootstrap_workers as hub_bootstrap_workers
from hubofhubs.recall.context_bootstrap_support import flag_enabled, optional_positive_int
from hubofhubs.recall.card_store_mounts import parse_mount_mode
from hubofhubs.recall.context_card_ceilings import MAX_CARDS_PER_FOLDER_DEFAULT, positive_int

from .walk_settings import STORE_DIR_NAME, skip_dirs_from_env

__all__ = [
    "ENV_MAX_CARDS", "STORE_DIR_NAME", "bootstrap_max_cards", "card_mount_mode", "bootstrap_max_files", "bootstrap_workers", "folder_review_enabled",
    "max_cards_from_env", "max_cards_per_folder", "prune_dead_cards", "skip_dirs_from_env", "skip_nested_repos",
]

ENV_MAX_CARDS = "QAR_MAX_CARDS"


def max_cards_from_env(env: Optional[dict] = None) -> int:
    """The configured card limit: ``QAR_MAX_CARDS``, else 1,000. ``0`` means no limit. Never raises."""
    return parse_max_cards((os.environ if env is None else env).get(ENV_MAX_CARDS), ENV_MAX_CARDS)


def card_mount_mode(env: Optional[dict] = None) -> str:
    """``QAR_CARD_MOUNT``: ``mount`` (read allowed stores in place) or ``copy`` (the legacy copy-import; the default)."""
    return parse_mount_mode((os.environ if env is None else env).get("QAR_CARD_MOUNT"))


def bootstrap_workers() -> int:
    """How many bootstrap chunks may hold a model call open at once: ``QAR_BOOTSTRAP_WORKERS``, else the deployment's
    ``QAR_MAX_PARALLEL`` budget (the same one the orchestrator's own fan-out reads), else 8."""
    return hub_bootstrap_workers([(name, os.getenv(name, "")) for name in ("QAR_BOOTSTRAP_WORKERS", "QAR_MAX_PARALLEL")])


def bootstrap_max_files() -> Optional[int]:
    """``QAR_BOOTSTRAP_MAX_FILES``: a ceiling on the files one bootstrap pass walks. None (the default) means no limit."""
    return optional_positive_int("QAR_BOOTSTRAP_MAX_FILES", os.getenv("QAR_BOOTSTRAP_MAX_FILES"))


def bootstrap_max_cards() -> Optional[int]:
    """``QAR_BOOTSTRAP_MAX_CARDS``: a ceiling on the cards one bootstrap pass writes. None (the default) means no limit."""
    return optional_positive_int("QAR_BOOTSTRAP_MAX_CARDS", os.getenv("QAR_BOOTSTRAP_MAX_CARDS"))


def max_cards_per_folder() -> int:
    """``QAR_BOOTSTRAP_MAX_CARDS_PER_FOLDER``: topic cards one folder may hold (default 8, ``0`` means no rule)."""
    return positive_int("QAR_BOOTSTRAP_MAX_CARDS_PER_FOLDER", os.getenv("QAR_BOOTSTRAP_MAX_CARDS_PER_FOLDER"),
                        MAX_CARDS_PER_FOLDER_DEFAULT)


def skip_nested_repos() -> bool:
    """``QAR_SKIP_NESTED_REPOS``: whether a directory with its own VCS metadata is excluded automatically (default yes)."""
    return flag_enabled(os.getenv("QAR_SKIP_NESTED_REPOS"))


def folder_review_enabled() -> bool:
    """``QAR_FOLDER_REVIEW``: whether the folder relevance review runs at all (default yes)."""
    return flag_enabled(os.getenv("QAR_FOLDER_REVIEW"))


def prune_dead_cards() -> bool:
    """``QAR_PRUNE_DEAD_CARDS``: whether a bootstrap or refresh removes cards the corpus no longer indexes (default yes).

    Set it to ``0`` for a deployment that would rather keep a stale index than lose a card."""
    return flag_enabled(os.getenv("QAR_PRUNE_DEAD_CARDS"))
