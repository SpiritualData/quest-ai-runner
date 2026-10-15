"""BASELINE: compare (mtime, size) per file, the cheap check without hashing. The floor sha256 must beat:
it misses an edit that keeps the size and restores the mtime, and flags a file that was merely touched."""
from pathlib import Path


def build(meter):  # noqa: ARG001
    def stat(root):
        return {str(p.relative_to(root)): (p.stat().st_mtime_ns, p.stat().st_size)
                for p in Path(root).rglob("*") if p.is_file()}

    def changed(before, after):
        old, new = stat(before), stat(after)
        return {rel for rel, sig in old.items() if new.get(rel) != sig}
    return changed
