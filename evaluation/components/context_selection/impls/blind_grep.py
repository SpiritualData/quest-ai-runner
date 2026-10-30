"""BASELINE: zero-model blind grep. Ranks every ``.py`` file by how many distinct task tokens (tokenized the
way the store does) appear in its text. No cards, no docstrings, no index: the floor any context layer
must beat to justify its existence."""
from quest_ai_runner.adapters.file_context_store import tokenize


def build(meter):  # noqa: ARG001
    texts = {}

    def select(task, corpus):
        key = str(corpus.root)
        if key not in texts:
            texts[key] = {rel: (corpus.root / rel).read_text(encoding="utf-8", errors="replace").lower()
                          for rel in corpus.files}
        tokens = tokenize(task)
        scores = {rel: sum(1 for t in tokens if t in text) for rel, text in texts[key].items()}
        # Ties broken by path so the ranking is deterministic.
        return [rel for rel, s in sorted(scores.items(), key=lambda kv: (-kv[1], kv[0])) if s > 0][:10]
    return select
