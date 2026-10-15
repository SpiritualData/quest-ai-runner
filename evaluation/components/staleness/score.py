"""Score one staleness case. The scorer builds ``before`` from ``files``, copies it to ``after``, applies the
case's ``ops`` (write / same_size_edit / touch / delete), and compares the returned set to ``expect_changed``.
``ok`` needs the exact set; ``tp`` / ``fp`` / ``fn`` feed the precision and recall in ``aggregate``."""
import os
import shutil
import tempfile
from pathlib import Path


def _apply(after, op):
    p = after / op["path"]
    st = p.stat() if p.exists() else None
    kind = op["op"]
    if kind == "write":
        p.write_text(op["content"])
    elif kind == "same_size_edit":   # new bytes, same length, mtime put back: only a content hash sees it
        old = p.read_text()
        new = op["content"]
        assert len(new) == len(old), "same_size_edit must keep the length"
        p.write_text(new)
        os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns))
    elif kind == "touch":            # mtime moves, bytes do not
        os.utime(p, (st.st_atime + 100, st.st_mtime + 100))
    elif kind == "delete":
        p.unlink()
    else:
        raise ValueError(kind)


def score_case(fn, case):
    work = Path(tempfile.mkdtemp(prefix="qar_stale_"))
    try:
        before, after = work / "before", work / "after"
        for rel, text in case["files"].items():
            (before / rel).parent.mkdir(parents=True, exist_ok=True)
            (before / rel).write_text(text)
        shutil.copytree(before, after, copy_function=shutil.copy2)
        for op in case["ops"]:
            _apply(after, op)
        got, want = set(fn(before, after)), set(case["expect_changed"])
    finally:
        shutil.rmtree(work, ignore_errors=True)
    tp, fp, fn_ = len(got & want), len(got - want), len(want - got)
    return dict(ok=got == want, tp=tp, fp=fp, fn=fn_, got=",".join(sorted(got)) or "(none)",
                note="ok" if got == want else f"expected {sorted(want)}, got {sorted(got)}")


def aggregate(rows):
    tp, fp, fn_ = (sum(r.get(k, 0) for r in rows) for k in ("tp", "fp", "fn"))
    precision = tp / (tp + fp) if tp + fp else 1.0
    recall = tp / (tp + fn_) if tp + fn_ else 1.0
    return f"precision {precision:.2f}, recall {recall:.2f} (tp {tp}, fp {fp}, fn {fn_})"
