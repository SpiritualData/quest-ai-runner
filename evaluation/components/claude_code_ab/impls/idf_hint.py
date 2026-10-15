"""ALTERNATIVE (offline arm): the warm hint, built the way ``context_selection`` builds cards (one deterministic card per ``.py``
file, scored with the store's IDF over docstrings and paths), top 3 files. Zero model calls."""
import common
from context_selection.corpus import corpus_for
from context_selection.impls import idf_docstrings


def build(meter):
    select = idf_docstrings.build(meter)

    def locate(case):
        return {"answer": ", ".join(select(case["task"], corpus_for(common.REPO))[:3]), "turns": None}
    return locate
