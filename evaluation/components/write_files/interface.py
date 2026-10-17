"""The file-write function. Every solution is ONE function with this signature:

    write(kind: str, args: dict) -> dict

A consumer lets the brain edit files inside one corpus root. The contract is a security boundary: no
write lands outside the root whatever the path claims, a refused or failed write leaves the old content
alone, and every overwrite is reversible (the replaced content is kept). The component builds a scratch
tree (a corpus root, a sibling "outside" folder holding a sentinel file, and a backup folder), attempts
one write and reports what happened on disk. Offline, no model, no network.

Kind ``write``, args:
  files            {relative path: text} created inside the root first
  symlink          optional {"name": link name inside root} pointing at the outside folder
  path             the path handed to the writer; ``{root}`` and ``{outside}`` expand to absolute paths
  content          text to write
  backups          bool (default true), ``backup_blocked`` makes the backup folder unwritable
  allow_create     bool (default true), ``max_write_bytes`` optional ceiling
Returns {"ok": the writer said so, "created": bool, "outside_intact": nothing outside the root changed,
"final": text now at the target inside the root or None, "backup_kept": a backup holds the replaced text}.

An implementation module in ``impls/`` exposes ``build(meter) -> write``. No solution here calls a model.
"""
