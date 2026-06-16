"""Shared directory-pruning helpers for the file-indexing adapters.

Implements the same "what to skip" logic that fd-find and ripgrep apply by
default: a hardcoded baseline of directories that are never source code, merged
with any simple directory-name patterns found in the project's ``.gitignore``.

This module is stdlib-only — no third-party dependencies — so the core package
stays zero-dependency.  The gitignore parser handles only bare directory names
and ``name/`` forms; complex patterns (wildcards, negations, embedded slashes)
are intentionally ignored and handled by the hardcoded baseline instead.
"""
from __future__ import annotations

from pathlib import Path
from typing import Set


# Baseline directories to skip regardless of project config.  Modelled after
# the defaults used by fd-find, ripgrep, and real-world deployment experience.
# Only *non-dot* names appear here because the walk loops already prune any
# directory whose name starts with "." via ``not d.startswith(".")``.
# Dot-prefixed names are listed too so code that omits the dot-guard still
# gets them (belt-and-suspenders).
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
    # Mobile SDKs and toolchains — large binary-heavy trees, never source code
    "Android", ".android",
    # Test / coverage
    "coverage", ".nyc_output", ".coverage",
    # Runner context store
    ".quest-context",
    # General hidden / tool caches
    ".cache", ".sass-cache",
}


def _gitignore_skip_dirs(root: Path) -> Set[str]:
    """Return bare directory names listed in ``root/.gitignore``.

    Parses only the safe subset: non-negated patterns that contain no
    wildcards and no embedded path separators.  Those patterns almost always
    correspond directly to a directory name the project author wants ignored
    (e.g. ``Android/``, ``jdk``, ``build/``).

    Patterns with wildcards, negations (``!``), or embedded slashes are
    silently skipped — they need a full gitignore engine and are rare as
    directory-skip hints.

    Never raises: a missing, unreadable, or malformed ``.gitignore`` returns
    an empty set.
    """
    extra: Set[str] = set()
    try:
        text = (root / ".gitignore").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return extra
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("!"):
            continue
        # Strip leading/trailing slashes to get the bare name.
        inner = line.lstrip("/").rstrip("/")
        # Skip path-specific patterns (contain a slash in the middle).
        if "/" in inner:
            continue
        # Skip glob patterns — they need a full matcher.
        if any(c in inner for c in ("*", "?", "[")):
            continue
        if inner:
            extra.add(inner)
    return extra


def effective_skip_dirs(root: Path) -> Set[str]:
    """Directories to prune when walking *root*.

    Returns the union of the hardcoded baseline and any simple names parsed
    from ``root/.gitignore``.  The result is cached by callers — call once
    per adapter instance, not once per ``os.walk`` call.
    """
    return _BASE_SKIP_DIRS | _gitignore_skip_dirs(root)
