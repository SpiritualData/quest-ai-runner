"""CURRENT: the production ``FilesWriter`` (``adapters/files_writer.py``) driven on a scratch tree. The
only fakes are the temp directories themselves. Zero model calls, zero network."""
import os
import tempfile
from pathlib import Path

from quest_ai_runner.adapters.files_writer import FilesWriter

SENTINEL = "OUTSIDE-ORIGINAL"


def scratch_tree(args):
    base = Path(tempfile.mkdtemp(prefix="qar_wf_"))
    root, outside = base / "corpus", base / "outside"
    root.mkdir()
    outside.mkdir()
    (outside / "secret.txt").write_text(SENTINEL)
    for rel, text in (args.get("files") or {}).items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    if args.get("symlink"):
        os.symlink(outside, root / args["symlink"])
    blocker = base / "blocker"
    if args.get("backup_blocked"):
        blocker.write_text("a file, so a folder cannot be made under it")
        backups = blocker / "b"
    else:
        backups = base / "backups"
    return base, root, outside, backups


def observe(base, root, outside, backups, rel_target, res, old_text):
    kept = False
    if backups.is_dir():
        kept = any(f.read_text() == old_text for f in backups.iterdir() if f.is_file()) if old_text is not None else False
    target = root / rel_target if rel_target else None
    inside = target is not None and target.resolve().is_relative_to(root.resolve())
    final = target.read_text() if inside and target.is_file() else None
    untouched = ((outside / "secret.txt").read_text() == SENTINEL and sorted(p.name for p in outside.iterdir()) == ["secret.txt"]
                 and sorted(p.name for p in base.iterdir() if p.name not in ("backups", "blocker")) == ["corpus", "outside"])
    return {"ok": bool(res["ok"]), "created": bool(res["created"]), "outside_intact": untouched,
            "final": final, "backup_kept": kept}


def build(meter):  # noqa: ARG001
    def write(kind, args):
        if kind != "write":
            raise ValueError(kind)
        base, root, outside, backups = scratch_tree(args)
        path = args["path"].replace("{root}", str(root)).replace("{outside}", str(outside))
        old = (args.get("files") or {}).get(args["path"])
        kw = {"backup_dir": str(backups), "backups_enabled": args.get("backups", True),
              "allow_create": args.get("allow_create", True)}
        if "max_write_bytes" in args:
            kw["max_write_bytes"] = args["max_write_bytes"]
        res = FilesWriter(str(root), **kw).write_file(path, args["content"])
        rel = args["path"] if not os.path.isabs(path) else (os.path.relpath(path, root) if path.startswith(str(root) + os.sep) else None)
        return observe(base, root, outside, backups, rel, {"ok": res.ok, "created": res.created}, old)
    return write
