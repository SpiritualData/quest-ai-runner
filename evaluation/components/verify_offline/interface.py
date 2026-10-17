"""The offline-suite check function. Every solution is ONE function with this signature:

    verify(kind: str, args: dict) -> dict

The test suite is how a change proves it broke nothing, but running all of it is not the metric: a full run
is slow, parts of it reach real model providers, and one run was killed mid-flight for spend (CLAUDE.md,
2026-09-19). What can be checked cheaply and honestly on every change is that the suite is still COLLECTABLE
(every test file imports, parametrisation resolves, nothing is syntactically broken; a missing fixture only
fails at setup, which collection cannot see), that no test silently
disappeared, and that no test file grew past the module-size ceiling without a waiver. Nothing here runs a
test body. Kinds:

  shard    {pattern, min_collected}  -> {"files", "collected", "errors", "meets_floor"}: collect every file
                                        matching the glob (relative to the repo) one file at a time
  fixture  {files}                   -> {"collected", "errors"}: collect a synthetic tests folder written from
                                        ``files`` ({name: source}); seeds known-broken files to prove the
                                        collector sees them
  coverage {shards}                  -> {"uncovered": test files whose first letter after test_ is in no shard}
  size     {}                        -> {"oversize": [files over the ceiling that carry no S4 waiver]}

An implementation module in ``impls/`` exposes ``build(meter) -> verify``. No solution calls a model.
"""
