"""Vendored third-party code.

Every module in this package is a COPY of code from another open-source project, kept here
verbatim-except-where-noted rather than taken as a runtime dependency. Each module states, in a
header comment: the source repository, the exact file and commit it came from, that project's
license, the date it was vendored, and precisely what was trimmed or adapted for use here (which
is what Apache-2.0 §4(b) requires of a modified file).

Vendoring is deliberate and rare. It is the right call when the upstream logic is (a) small,
stdlib-only and self-contained, (b) not separately published as a package, and (c) hard-won —
full of specific fixes for real model misbehaviour that an approximation would silently lose.
When those do not all hold, depend on the package instead.

Rules for anything in here:
  * Do not "clean up" vendored code to match this repo's style. Keeping it close to upstream is
    what makes re-syncing a future upstream fix possible.
  * Any behavioural change goes in the header's "adapted" list, not silently into the body.
  * Attribution also belongs in the repo's top-level ``NOTICE``.
"""
