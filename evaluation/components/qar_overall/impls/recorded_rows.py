"""CURRENT: read every sub-metric from the committed results log, counting an arm only on a current row.

The cheap tier reads an arm's latest row of any size, the full tier its latest row on the whole set; a row counts only
when it has a commit, was recorded on committed code and on the cases now on disk, and no committed change to the
measured code came after it (``combine.current``). A stale, missing or planned arm is unmeasured. Zero model calls: this
arm runs nothing, it reads what the other arms recorded."""
from qar_overall import combine


def build(meter):  # noqa: ARG001
    return combine.read
