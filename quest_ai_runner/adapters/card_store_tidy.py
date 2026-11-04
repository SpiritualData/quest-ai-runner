"""QAR's words for the hub's repository tidy: the ``quest-ai-runner cards tidy`` commands and messages, and the declared implementation.

The tidy itself (plan, export, apply, undo through the ``CardRepository`` port) is the hub's, ``hubofhubs.recall.card_store_tidy``.
What names QAR stays here: the command lines a person is told to run, the lane's config file, the ``card_tidy`` setting and the
messages built from them. ``repository_tidy`` is the ``repair-a-card-store`` implementation QAR declares over the hub's class.
"""
from __future__ import annotations

import os
from typing import Any

from esoi import implementation
from hubofhubs.recall.card_store_tidy import RepositoryTidy, StorePlan, TidyRun, is_local_folder, split_steps


@implementation("repair-a-card-store", "repository_tidy")
def repository_tidy(repo: Any, cards_dir: Any, **options: Any) -> RepositoryTidy:
    """QAR's ``repair-a-card-store`` implementation: the hub's repository tidy, carried out through the ``CardRepository`` port."""
    return RepositoryTidy(repo, cards_dir, **options)


def lane_args(repo: Any, cards_dir: Any) -> str:
    """The arguments that point ``quest-ai-runner cards ...`` at this store: its card folder for a local folder,
    else the lane's config file (the Quest backend and Qdrant are reached through it)."""
    if is_local_folder(repo):
        return f"--cards-dir {cards_dir}"
    return f"--config {os.environ.get('QAR_CONFIG_FILE') or '<the lane config file>'}"


def apply_command(repo: Any, cards_dir: Any, *, merge_topics: bool = False) -> str:
    return (f"quest-ai-runner cards tidy {lane_args(repo, cards_dir)} --apply"
            + (" --merge-topics" if merge_topics else ""))


REVERSIBLE = "Every card it touches is exported first, and --undo with the manifest it prints restores it."


def tidied_message(run: TidyRun, undo_hint: str, joined: int = 0) -> str:
    """What a tidy did; ``joined`` of the removed cards were the second step's (``SECOND_STEP``)."""
    text = f"Tidied {run.removed - joined:,} context cards (copies and cards about excluded folders)"
    if joined:
        text += (f", then, as a second step to stay under the card limit, joined {joined:,} "
                 f"{'card that pinned' if joined == 1 else 'cards that pinned'} the same files under another name "
                 "onto their keeper")
    return text + f"; undo with: {undo_hint}"


def planned_message(plan: StorePlan, apply_hint: str, *, total: int = 0, merge_hint: str = "") -> str:
    """One plain sentence of what the tidy would remove (and, for a store still over its limit after it, what the
    second step would join), then how to apply it, how to make it automatic, and that it can be undone."""
    firsts, seconds = split_steps(plan)
    removed = len(firsts)
    why = f" because {plan.why}" if plan.why else ""
    text = (f"The context store needs a tidy{why}: removing {removed:,} copies and cards about excluded folders"
            + (f" ({StorePlan(entries=firsts).summary()})" if removed else ""))
    if total:
        text += f" would leave {total - removed:,} cards"
    else:
        text += " would make it smaller"
    if seconds:
        joined = len(seconds)
        text += (f", and a second step, joining {joined:,} {'card that pins' if joined == 1 else 'cards that pin'} "
                 f"the same files under another name onto their keeper, would bring the store to "
                 f"{total - removed - joined:,} cards")
    text += "; nothing was changed."
    if seconds and merge_hint:
        text += f" To apply both steps: {merge_hint} (the first step alone: {apply_hint})."
    else:
        text += f" To apply: {apply_hint}."
    return (text + ' To tidy automatically, set card_tidy = "apply" in the lane config. ' + REVERSIBLE)
