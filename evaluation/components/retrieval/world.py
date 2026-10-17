"""Shared fixture helpers: load the synthetic corpus and render a document the ways the sources hold it."""
import json
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent


def load_corpus():
    return json.loads((HERE / "corpus.json").read_text())


def markdown(doc):
    """A document as a markdown file: title, summary, then its sections."""
    parts = [f"# {doc['title']}", "", doc["summary"], ""]
    for section in doc["sections"]:
        parts += [f"## {section['heading']}", section["text"], ""]
    return "\n".join(parts)


def body_lines(doc):
    return [line for line in markdown(doc).splitlines() if line.strip()]


def write_tree(docs, root=None):
    """The documents as ``<id>.md`` files under a fresh temp directory; returns the directory."""
    root = Path(root or tempfile.mkdtemp(prefix="qar_retrieval_"))
    for doc in docs:
        (root / f"{doc['id']}.md").write_text(markdown(doc), encoding="utf-8")
    return root


def doc_id_from_path(path):
    """``alias:dir/<id>.md`` or ``<id>.md`` -> ``<id>``."""
    return Path(str(path).split(":", 1)[-1]).stem


class Lazy:
    """Build the adapter once, on the first question, keyed by the corpus it was built for."""

    def __init__(self, factory):
        self.factory, self.value = factory, None

    def get(self, corpus):
        if self.value is None:
            self.value = self.factory(corpus)
        return self.value
