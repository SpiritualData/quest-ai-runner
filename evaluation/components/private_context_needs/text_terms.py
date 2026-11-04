"""Shared offline text helpers for the arms: tokens, IDF weights over a case's candidates, cosine."""
import math
import re
from collections import Counter

STOP = frozenset("the a an and or of to in on for with is are be this that it as at by from we you i our your not no can will should have has do "
                 "does what how when which if then so but also all any more was were been into than them they their there here".split())


def tokens(text):
    return [w for w in re.findall(r"[a-z][a-z0-9]{2,}", (text or "").lower()) if w not in STOP]


def idf_weights(docs):
    """``{term: idf}`` over a list of token lists."""
    df = Counter()
    for d in docs:
        df.update(set(d))
    n = len(docs)
    return {t: math.log((n + 1) / (c + 1)) + 1.0 for t, c in df.items()}


def vector(toks, idf):
    tf = Counter(toks)
    return {t: (1 + math.log(c)) * idf.get(t, 1.0) for t, c in tf.items()}


def cosine(a, b):
    dot = sum(v * b.get(t, 0.0) for t, v in a.items())
    na, nb = math.sqrt(sum(v * v for v in a.values())), math.sqrt(sum(v * v for v in b.values()))
    return dot / (na * nb) if na and nb else 0.0
