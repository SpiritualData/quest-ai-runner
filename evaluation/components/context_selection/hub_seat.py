"""The arms of the hub's ``retrieve-context`` seat (``hubofhubs.retrieval``) as arms of the one context-selection metric.

An arm here owns no retrieval code: it builds the hub's retriever class, hands it the corpus's files (and, for a learned arm on the repository
corpus, the hub's own co-change history, every commit of which is older than every topic case), and returns the files of the units it
answers with, best first. Nothing is copied from the hub; an improvement to a hub arm is an improvement here the next time the arm runs.

A private case's corpus is the case's candidate items, one file each (``private_cases.py``). Learned arms cannot train on a corpus of
twenty items, so on those cases they train on the earlier private cases (``private_learned.py``) or fall back to what the hub arm does with no history.
"""
import common
from context_selection.store_cost import store_charged


def files_of(corpus):
    return {rel: corpus.path_of(rel).read_text(encoding="utf-8", errors="replace") for rel in corpus.files}


def is_repository(corpus):
    return corpus.root == common.REPO.resolve()


def hub_history():
    """The hub's co-change history (480 commits, 2026-06-05 to 2026-10-06), all older than the first topic case of this metric (2026-10-29)."""
    from context_selection import topic_cases
    topic_cases.hub_helpers()        # puts the hub's evaluation folder on sys.path
    from cards_from_files import topic_cases as hub_cases
    return hub_cases.load_history()


def delegate(retriever, learned=False, abstain=False, budget_tokens=4000):
    """``select(task, corpus)`` over a hub retriever instance. A learned arm gets the hub history on the repository corpus."""
    state = {}
    private = PrivateTrained(abstain=abstain) if learned else None

    def select(task, corpus):
        if learned and not is_repository(corpus):
            return private.select(task, corpus)
        history = ()
        if learned and is_repository(corpus):
            state.setdefault("history", hub_history())
            history = state["history"]
        hits = retriever.retrieve(task, files_of(corpus) if retriever.corpus is None or state.get("key") != corpus.key else None,
                                  budget_tokens=budget_tokens, history=history)
        state["key"] = corpus.key
        return list(dict.fromkeys(h.file for h in hits))
    return select


def build_arm(retriever_class, learned=False, abstain=False):
    def build(meter):
        return store_charged(delegate(retriever_class(), learned=learned, abstain=abstain), meter, cards=lambda corpus: 0)
    return build


class PrivateTrained:
    """The hub's learned reranker, trained on QAR's private labels split in time.

    The hub's ``LearnedRerank`` learns from commits whose files sit in one corpus; a private case is a corpus of its own twenty items, so this
    builds the same candidate features (the hub's ``HybridRrf`` and ``Candidates``) for each EARLIER private case, labels each candidate by the
    case's needed items, and fits the same logistic regression. The private cases carry no timestamp or quest id, so their order by case id (the
    order they were collected in) is the time split: a case is scored by a model trained only on the cases with a smaller id, never on itself or on
    a later one. Everything stays in this process's memory; nothing is written, copied or returned except the ranking of the case's own items."""
    MIN_ROWS, MIN_POSITIVES, MAX_TRAIN_CASES = 60, 15, 60

    def __init__(self, abstain=False):
        from hubofhubs.api import HybridRrf
        self.hybrid_class, self.abstain = HybridRrf, abstain
        self.cases = None
        self.rows = {}            # case id -> (feature rows, labels), computed once
        self.trained_on = {}      # case id -> how many earlier cases its model saw (counts only)

    def load(self):
        if self.cases is None:
            from context_selection import private_cases
            folders = private_cases.present_folders()
            ordered = sorted(private_cases.load_all(folders), key=lambda c: c["id"]) if folders else []
            self.cases = ordered
            self.by_task = {c["task"]: i for i, c in enumerate(ordered)}
        return self.cases

    def features(self, case):
        from context_selection import private_cases
        from hubofhubs.api import Candidates
        if case["id"] not in self.rows:
            corpus, owner = private_cases.item_corpus(case)
            hybrid = self.hybrid_class()
            hybrid.prepare(files_of(corpus), ())
            wanted = sorted(rel for rel, item in owner.items()
                            if next(c for c in case["candidates"] if c["item_id"] == item)["label"] == "needed")
            cands = Candidates(hybrid, case["task"], hybrid.corpus, minus=wanted)
            rows = [r[:8] + [0.0] + r[9:] for r in map(list, cands.matrix)]   # recency is not learned, as in the hub arm
            labels = [int(hybrid.corpus.units[i].file in set(wanted)) for i in cands.pool]
            self.rows[case["id"]] = (rows, labels)
        return self.rows[case["id"]]

    def model_for(self, position):
        import numpy as np
        from sklearn.linear_model import LogisticRegression
        earlier = self.cases[max(0, position - self.MAX_TRAIN_CASES):position]
        rows, labels = [], []
        for case in earlier:
            r, l = self.features(case)
            rows += r
            labels += l
        self.trained_on[self.cases[position]["id"]] = len(earlier)
        if len(rows) < self.MIN_ROWS or sum(labels) < self.MIN_POSITIVES:
            return None
        return LogisticRegression(C=1.0, max_iter=2000, class_weight="balanced").fit(np.array(rows), np.array(labels))

    def select(self, task, corpus):
        import numpy as np
        from hubofhubs.api import CONFIDENCE_FLOOR, confidence
        from hubofhubs.api import Candidates
        self.load()
        position = self.by_task.get(task)
        hybrid = self.hybrid_class()
        hybrid.prepare(files_of(corpus), ())
        if self.abstain and confidence(hybrid, task) < CONFIDENCE_FLOOR:
            return []
        model = self.model_for(position) if position is not None else None
        cands = Candidates(hybrid, task, hybrid.corpus)
        if model is None or not len(cands.matrix):
            ranked = hybrid.ranking(task)
        else:
            scores = model.decision_function(np.array([r[:8] + [0.0] + r[9:] for r in map(list, cands.matrix)]))
            ranked = sorted(zip(cands.pool, scores), key=lambda kv: (-kv[1], kv[0]))
        return list(dict.fromkeys(hybrid.corpus.units[i].file for i, _ in ranked))[:10]
