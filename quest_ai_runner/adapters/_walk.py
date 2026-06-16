"""Shared directory-pruning helpers for the file-indexing adapters.

Implements the same skip logic that fd-find and ripgrep apply by default:

  1. A hardcoded baseline of directories that are never source code
     (language-agnostic: covers JS, Python, Rust, Go, JVM, mobile SDKs, …).
  2. Any bare directory name found in a ``.gitignore`` file — read at the
     root *and* in every sub-directory as the walk descends, so nested
     ``.gitignore`` files are honoured exactly as git does.

This module is stdlib-only (no third-party deps), so the core package stays
zero-dependency.  The gitignore parser handles only the safe subset of
patterns: bare names and ``name/`` forms with no wildcards, negations, or
embedded slashes.  Complex patterns need a full gitignore engine; they are
uncommon as directory-skip hints and are left to the hardcoded baseline.
"""
from __future__ import annotations

from pathlib import Path
from typing import Set


# Baseline directories that are never source code, regardless of what a
# project's .gitignore says.  Modelled after fd-find and ripgrep defaults.
# Both dot-prefixed and bare names are listed so callers that skip the
# ``not d.startswith(".")`` dot-guard still prune them (belt-and-suspenders).
_BASE_SKIP_DIRS: Set[str] = {
    # VCS
    ".git",
    # JavaScript / TypeScript
    "node_modules",
    ".next", ".turbo", ".svelte-kit", "storybook-static", ".parcel-cache",
    # Python
    "__pycache__", ".venv", "venv", ".eggs", ".mypy_cache", ".pytest_cache",
    # Build & distribution (language-agnostic)
    "dist", "build", "_build",
    "target",          # Rust (cargo), Maven, SBT — can be enormous
    "vendor",          # Go modules, PHP Composer, Ruby Bundler
    # Java / JVM
    ".gradle", ".m2",
    # Mobile SDKs and toolchains
    "Android", ".android",
    # Test / coverage
    "coverage", ".nyc_output", ".coverage",
    # Runner context store
    ".quest-context",
    # General hidden / tool caches
    ".cache", ".sass-cache",
}


def parse_gitignore_names(directory: Path) -> Set[str]:
    """Return bare directory names listed in ``directory/.gitignore``.

    Parses only the safe subset: non-negated patterns with no wildcards and
    no embedded path separators.  Those patterns almost always correspond to
    a directory name the project author wants ignored (e.g. ``Android/``,
    ``jdk``, ``build/``).

    Call this once per directory visited during ``os.walk`` — passing
    ``Path(dirpath)`` — so nested ``.gitignore`` files are honoured as git
    does, not just the root one.

    Patterns with wildcards, negations (``!``), or embedded slashes are
    silently skipped — they need a full gitignore engine and are rare as
    directory-skip hints.

    Never raises: a missing, unreadable, or malformed ``.gitignore`` returns
    an empty set.
    """
    names: Set[str] = set()
    try:
        text = (directory / ".gitignore").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return names
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("!"):
            continue
        # Strip leading/trailing slashes to get the bare name.
        inner = line.lstrip("/").rstrip("/")
        # Skip path-specific patterns (slash in the middle).
        if "/" in inner:
            continue
        # Skip glob patterns — they need a full matcher.
        if any(c in inner for c in ("*", "?", "[")):
            continue
        if inner:
            names.add(inner)
    return names


def effective_skip_dirs(root: Path) -> Set[str]:
    """Baseline skip set for *root*: hardcoded defaults + root-level .gitignore.

    Cache this once per adapter instance and pass it into ``prune_dirnames``
    on every ``os.walk`` iteration so nested ``.gitignore`` files are also
    honoured without re-reading the root on every step.
    """
    return _BASE_SKIP_DIRS | parse_gitignore_names(root)


def prune_dirnames(dirnames: list, *, current: Path, base_skip: Set[str]) -> None:
    """Prune *dirnames* in-place for one ``os.walk`` step.

    Removes any entry that:
    - is in the *base_skip* set (baseline + root .gitignore, pre-computed), OR
    - is listed in ``current/.gitignore`` (nested .gitignore, read per step), OR
    - starts with "." (hidden directories are never source code).

    Mutates *dirnames* in-place as required by ``os.walk`` to prevent descent.
    Pass the caller's own exclusion logic (e.g. cards_dir) AFTER this call.
    """
    local = parse_gitignore_names(current)
    dirnames[:] = [
        d for d in dirnames
        if d not in base_skip
        and d not in local
        and not d.startswith(".")
    ]
