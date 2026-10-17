"""BASELINE: the obvious writer. Joins the path onto the root and writes it: no containment check, no
protected names, no size ceiling, no backup. The floor the real writer must beat. Zero model calls."""
import os

from write_files.impls.bounded_writer import observe, scratch_tree


def build(meter):  # noqa: ARG001
    def write(kind, args):
        if kind != "write":
            raise ValueError(kind)
        base, root, outside, backups = scratch_tree(args)
        path = args["path"].replace("{root}", str(root)).replace("{outside}", str(outside))
        target = os.path.join(str(root), path)
        existed = os.path.isfile(target)
        try:
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(target, "w") as fh:
                fh.write(args["content"])
            ok = True
        except OSError:
            ok = False
        old = (args.get("files") or {}).get(args["path"])
        rel = os.path.relpath(target, root) if target.startswith(str(root) + os.sep) else None
        return observe(base, root, outside, backups, rel, {"ok": ok, "created": ok and not existed}, old)
    return write
