"""The gather policy shared by every retrieval implementation, so a score differs only by what the adapter
can find and read. It is deliberately plain (keyword terms, one search per term, rarity weighting): the
metric is the ADAPTER's recall under a fixed policy, not a smarter planner's. Zero model calls."""
import re

STOP = frozenset("""about above after again also been before being between both could does doing down each from have
having here into just like more most much must once only other over same should some such than that their them then
there these they this those through under until very what when where which while whom will with would your""".split())
SUFFIXES = ("ing", "ed", "es", "s")
MAX_TERMS = 8


def stem(word):
    for suffix in SUFFIXES:
        if word.endswith(suffix) and len(word) - len(suffix) >= 4:
            return word[:-len(suffix)]
    return word


def terms(question):
    """Distinct stemmed content words of the question, in order (4+ letters, no stop words)."""
    seen, out = set(), []
    for word in re.findall(r"[a-z0-9]+", question.lower()):
        if len(word) < 4 or word in STOP:
            continue
        root = stem(word)
        if root not in seen:
            seen.add(root)
            out.append(root)
    return out[:MAX_TERMS]


def rank(found):
    """``found`` maps term -> doc ids that matched it. A term matching few documents counts more
    (1 / number of matching documents); ties go to the document matching more distinct terms, then id."""
    score, distinct = {}, {}
    for term, ids in found.items():
        ids = set(ids)
        for doc_id in ids:
            score[doc_id] = score.get(doc_id, 0.0) + 1.0 / len(ids)
            distinct[doc_id] = distinct.get(doc_id, 0) + 1
    return sorted(score, key=lambda d: (-score[d], -distinct[d], d))


def gather(question, search, read, k=3):
    """``search(term) -> [doc ids]`` and ``read(doc id) -> text`` are the adapter-specific halves."""
    found = {}
    for term in terms(question):
        try:
            found[term] = list(search(term) or [])
        except Exception:  # noqa: BLE001  an adapter is expected to fail soft; a raise scores as no hits
            found[term] = []
    ids = rank(found)
    texts = {}
    for doc_id in ids[:k]:
        try:
            texts[doc_id] = read(doc_id) or ""
        except Exception:  # noqa: BLE001
            texts[doc_id] = ""
    return {"ids": ids, "texts": texts}
