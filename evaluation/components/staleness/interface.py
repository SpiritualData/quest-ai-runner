"""The staleness function. Every solution is ONE function with this signature:

    changed(before: Path, after: Path) -> set[str]

``before`` is the directory a context index was built over, ``after`` the same tree later. Returns the
repo-relative paths whose content is no longer what the index captured (edited or deleted). A path that was
only touched (mtime changed, bytes identical) is NOT stale, and an edit that keeps the size and restores the
mtime IS. How a solution remembers ``before`` (a store of fingerprints, a stat table) is its own business.

An implementation module in ``impls/`` exposes ``build(meter) -> changed``. No solution here calls a model.
"""
