"""CURRENT: the production freshness check. ``FileContextStore`` pins each file with a sha256 / mtime
fingerprint when its card is written, and ``stale_cards_for(path)`` recomputes the hash to see whether the
file changed. The index is built over a private copy of ``before``; the ``after`` files are then laid over
that copy and every pinned path is asked. Zero model calls."""
import shutil
import tempfile
from pathlib import Path

from quest_ai_runner.adapters.file_context_store import FileContextStore


def build(meter):  # noqa: ARG001
    def changed(before, after):
        work = Path(tempfile.mkdtemp(prefix="qar_stale_idx_"))
        try:
            live = work / "live"
            shutil.copytree(before, live, copy_function=shutil.copy2)
            store = FileContextStore(str(work / "cards"), repo_root=str(live), auto_bootstrap=False)
            paths = sorted(str(p.relative_to(live)) for p in live.rglob("*") if p.is_file())
            for i, rel in enumerate(paths):
                store._own_repo.write(f"card{i}", dict(id=f"card{i}", keywords=[rel], summary=rel,
                                                       files=[dict(path=rel, **store._fingerprint(rel))]))
            store._cache_dirty = True
            shutil.rmtree(live)                       # lay the "after" tree where the cards point
            shutil.copytree(after, live, copy_function=shutil.copy2)
            return {rel for rel in paths if store.stale_cards_for(rel)}
        finally:
            shutil.rmtree(work, ignore_errors=True)
    return changed
