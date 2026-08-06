"""Personal lexicon: the terms one person actually uses, ranked by TF-DF-IDF.

Given the documents a single person wrote (journal entries, reviews, goals, notes, chat
turns), this ranks the vocabulary that is *theirs*: the words and multi-word phrases they
use repeatedly and that are not simply common English or common to everybody else. The
output is a ranked term list, which is what a caller needs to bias a speech recognizer,
seed a query expander, or label a corpus.

The ranking is the same TF-DF-IDF heuristic this package already uses for sampling (see
``tfdfidf_sampling``), with the person taking the role of the cluster:

    score(t) = (1 + ln TF) * (1 + ln DF) * IDF(t)

    TF  total instances of the term across the person's documents
    DF  how many of their documents contain it (a term used once, in one place, is noise)
    IDF how distinctive the term is OUTSIDE this person, from a background source

Both counts are logarithmically damped on purpose. Undamped, one long entry or one
much-repeated ordinary word outranks the rare term that actually needs the boost: it is
distinctiveness that has to dominate here, not volume.

IDF comes from a ``BackgroundFrequency``, of which there are two useful kinds, and they
answer the same question from different directions:

    ``LanguageBaseline``  is this word common in the language at large?
    ``CorpusBackground``  do all the other people in this deployment use it too?

``CombinedBackground`` takes the LOWEST opinion of several sources, so a term has to look
distinctive on every available axis to rank. That is what stops a deployment's own house
vocabulary from ranking: a word every user writes is not personal to any of them, however
absent it is from general English.

Deliberately model-free and dependency-free. It is arithmetic over text, so it can run on
every write, inside a cache warm, or in a loop over an entire corpus without a budget.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from .english_word_bands import band_of

# IDF scale. Everything a background source returns lives on this scale, so several
# sources can be compared and combined. A word in the most common band contributes
# nothing at all (its score collapses to zero and it never reaches the output), which is
# how ordinary English drops out without needing a separate stopword pass at ranking time.
BAND_IDF = (0.0, 0.8, 1.6)
MAX_IDF = 3.0           # a term in no band: unknown to general English, so treated as rare
PHRASE_IDF_BONUS = 0.15  # per extra content word, see phrase_idf()

# Terms may not START or END on one of these. Interior ones are fine and necessary: "law
# of attraction" and "fear of failure" are exactly the kind of phrase worth keeping, while
# "of attraction" and "the fear" are not terms at all. This is a narrow function-word list,
# NOT a general stopword list: content words like "work" or "feel" must stay eligible as
# term edges ("deep work", "feel stuck").
EDGE_WORDS: frozenset = frozenset("""
a about after all am an and any are as at be been being but by can could did do does doing
for from had has have having he her hers him his i if in into is it its me might mine most
much must my no nor not of off on once one only or other our ours out over own same she so
some such than that the their theirs them then there these they this those to too us very
was we were what when where which while who whom whose why will with would you your yours
""".split())

DEFAULT_MAX_PHRASE_WORDS = 3
DEFAULT_MIN_WORD_CHARS = 3
DEFAULT_MIN_DOCUMENTS = 2
DEFAULT_MIN_OCCURRENCES = 2
DEFAULT_MAX_TERMS = 120
DEFAULT_SUBSUMPTION_RATIO = 1.25

# Unicode-aware word matcher: letters only (no digits, no underscores), with internal
# apostrophes and hyphens kept so "don't" and "self-worth" survive as single tokens. Digits
# are dropped deliberately: "2026" is not vocabulary, and a spelling hint cannot use it.
WORD_RE = re.compile(r"[^\W\d_]+(?:['’\-][^\W\d_]+)*", re.UNICODE)


def tokenize(text: str) -> List[str]:
    """Words of `text`, original casing preserved (the casing IS part of a spelling hint)."""
    return WORD_RE.findall(text or "")


def word_idf(word: str) -> float:
    """IDF of a single word from the built-in general-English bands."""
    band = band_of(word)
    return MAX_IDF if band < 0 else BAND_IDF[band]


def phrase_idf(words: Sequence[str]) -> float:
    """IDF of a term from its words, via the general-English bands.

    Single words read straight off the bands. For a phrase, the mean is taken over its
    CONTENT words only, since interior function words ("of" in "law of attraction") carry
    no information about how distinctive the phrase is and would only dilute it. A small
    per-word bonus then lifts the phrase above its own parts, because a multi-word term
    carries its parts' signal plus the fact that they occur together. That ordering is what
    lets `drop_subsumed_terms` retire the parts and spend the caller's budget on the phrase.
    """
    content = [w for w in words if w.lower() not in EDGE_WORDS] or list(words)
    mean = sum(word_idf(w) for w in content) / len(content)
    return mean * (1.0 + PHRASE_IDF_BONUS * (len(content) - 1))


class BackgroundFrequency:
    """How distinctive a term is outside one person's own documents.

    `idf` returns a value on the `BAND_IDF`/`MAX_IDF` scale, or None for "no opinion",
    which `CombinedBackground` skips rather than treating as agreement.
    """

    def idf(self, term: str) -> Optional[float]:  # pragma: no cover - interface
        raise NotImplementedError


class LanguageBaseline(BackgroundFrequency):
    """General-English baseline: common words are not distinctive, whoever wrote them.

    Needs no corpus and no warm-up, so it is the source that works on day one, for the
    very first user of a deployment. Its blind spots are equally structural: it knows only
    the bundled English bands, so a deployment's house vocabulary and any non-English
    function word look distinctive to it. `CorpusBackground` is what corrects both, which
    is why they are meant to be combined rather than chosen between.
    """

    def idf(self, term: str) -> Optional[float]:
        return phrase_idf(tokenize(term))


class CorpusBackground(BackgroundFrequency):
    """Cross-population baseline: a term everyone uses is personal to nobody.

    Built from how many members of the population use each term, which a consumer can
    accumulate as it computes lexicons rather than by scanning anything extra.

    Below `min_population` members the counts are too thin to mean anything (with five
    users, a term two of them use looks "rare" by arithmetic and common by eye), so the
    source abstains entirely and lets the language baseline answer alone.
    """

    def __init__(self, term_counts: Mapping[str, int], population: int,
                 min_population: int = 25, max_idf: float = MAX_IDF) -> None:
        self.term_counts = term_counts
        self.population = population
        self.min_population = min_population
        self.max_idf = max_idf

    def idf(self, term: str) -> Optional[float]:
        if self.population < self.min_population:
            return None
        users = self.term_counts.get(term.lower(), 0)
        # Normalized to the shared scale: used by everyone -> 0.0, used by nobody else ->
        # max_idf. Bounded this way so it stays comparable to the language bands however
        # large the population grows.
        span = math.log(self.population + 1)
        return self.max_idf * math.log((self.population + 1) / (users + 1)) / span


class CombinedBackground(BackgroundFrequency):
    """The lowest IDF any source is willing to give (sources with no opinion abstain)."""

    def __init__(self, *sources: BackgroundFrequency) -> None:
        self.sources = [s for s in sources if s is not None]

    def idf(self, term: str) -> Optional[float]:
        values = [v for v in (s.idf(term) for s in self.sources) if v is not None]
        return min(values) if values else None


@dataclass(frozen=True)
class LexiconTerm:
    """One ranked term. `term` carries the person's own dominant casing and spelling."""
    term: str
    score: float
    documents: int
    occurrences: int
    idf: float

    @property
    def word_count(self) -> int:
        return len(self.term.split())

    def as_dict(self) -> Dict:
        return {"term": self.term, "score": round(self.score, 4),
                "documents": self.documents, "occurrences": self.occurrences,
                "idf": round(self.idf, 4)}

    @classmethod
    def from_dict(cls, data: Mapping) -> "LexiconTerm":
        return cls(term=data["term"], score=float(data.get("score", 0.0)),
                   documents=int(data.get("documents", 0)),
                   occurrences=int(data.get("occurrences", 0)),
                   idf=float(data.get("idf", 0.0)))


def candidate_terms(words: Sequence[str], max_phrase_words: int,
                    min_word_chars: int) -> Iterable[Tuple[str, str]]:
    """Yield (key, surface) for every admissible term in one document's word sequence.

    `key` is the lowercased term used for counting; `surface` preserves what was written.

    Admissible means: it neither starts nor ends on a function word, and it carries at
    least one word of real length (or a capitalized one, so short proper nouns survive).
    That last condition is also the only thing holding back a non-English document, whose
    function words are absent from the English edge list: "de la" is rejected on length,
    though "de casa" is not. `CorpusBackground` is the real fix for that case.
    """
    n = len(words)
    for start in range(n):
        if words[start].lower() in EDGE_WORDS:
            continue
        for length in range(1, max_phrase_words + 1):
            end = start + length
            if end > n:
                break
            span = words[start:end]
            if span[-1].lower() in EDGE_WORDS:
                continue
            if not any(len(w) >= min_word_chars or w[:1].isupper() for w in span):
                continue
            surface = " ".join(span)
            yield surface.lower(), surface


def drop_subsumed_terms(ranked: List[LexiconTerm],
                        ratio: float = DEFAULT_SUBSUMPTION_RATIO) -> List[LexiconTerm]:
    """Remove a term that a better-ranked term already contains and accounts for.

    A hint has a hard budget, so spending three slots on "law", "attraction" and "law of
    attraction" wastes two of them. A shorter term is dropped only when a higher-ranked
    term contains it AND the shorter one barely occurs outside it (`ratio`): a word the
    person genuinely also uses on its own keeps its slot.
    """
    kept: List[LexiconTerm] = []
    for term in ranked:
        needle = f" {term.term.lower()} "
        if any(needle in f" {k.term.lower()} " and term.occurrences <= ratio * k.occurrences
               for k in kept):
            continue
        kept.append(term)
    return kept


def build_personal_lexicon(
    documents: Sequence[str],
    *,
    background: Optional[BackgroundFrequency] = None,
    max_phrase_words: int = DEFAULT_MAX_PHRASE_WORDS,
    min_word_chars: int = DEFAULT_MIN_WORD_CHARS,
    min_documents: int = DEFAULT_MIN_DOCUMENTS,
    min_occurrences: int = DEFAULT_MIN_OCCURRENCES,
    max_terms: int = DEFAULT_MAX_TERMS,
    subsumption_ratio: float = DEFAULT_SUBSUMPTION_RATIO,
) -> List[LexiconTerm]:
    """Rank one person's distinctive vocabulary from the documents they wrote.

    Args:
        documents: this person's own text, one string per source item. Each item is a
            separate document for DF purposes, so keep them separate: DF is what tells a
            term used across someone's life from a term used five times in one entry.
        background: distinctiveness source. Defaults to `LanguageBaseline`.
        max_phrase_words: longest term, in words.
        min_word_chars: a term needs one word at least this long, unless capitalized.
        min_documents: a term must appear in at least this many documents.
        min_occurrences: and occur at least this many times in total.
        max_terms: cap on the returned list.
        subsumption_ratio: see `drop_subsumed_terms`.

    Returns:
        Terms by descending score. Zero-IDF terms (ordinary English) never appear.

    `min_documents` defaults to 2 for a reason that matters wherever this feeds back into
    its own inputs. When the documents include dictated text, a one-off mis-recognition is
    a plausible-looking rare "term", and promoting it would teach the recognizer to produce
    it again. Requiring corroboration from a second, independent document is what breaks
    that loop: a slip happens once, a person's real vocabulary recurs.
    """
    background = background or LanguageBaseline()

    doc_counts: Dict[str, int] = {}
    total_counts: Dict[str, int] = {}
    surfaces: Dict[str, Dict[str, int]] = {}

    for document in documents:
        words = tokenize(document)
        if not words:
            continue
        seen_here: Set[str] = set()
        for key, surface in candidate_terms(words, max_phrase_words, min_word_chars):
            total_counts[key] = total_counts.get(key, 0) + 1
            surfaces.setdefault(key, {})
            surfaces[key][surface] = surfaces[key].get(surface, 0) + 1
            if key not in seen_here:
                seen_here.add(key)
                doc_counts[key] = doc_counts.get(key, 0) + 1

    ranked: List[LexiconTerm] = []
    for key, documents_with_term in doc_counts.items():
        occurrences = total_counts[key]
        if documents_with_term < min_documents or occurrences < min_occurrences:
            continue
        idf = background.idf(key)
        if not idf or idf <= 0:
            continue
        score = (1.0 + math.log(occurrences)) * (1.0 + math.log(documents_with_term)) * idf
        # Most-written form wins, so the hint carries the person's own capitalization; ties
        # break on the lowercase form for a deterministic result.
        variants = surfaces[key]
        surface = max(sorted(variants), key=lambda s: variants[s])
        ranked.append(LexiconTerm(term=surface, score=score,
                                  documents=documents_with_term,
                                  occurrences=occurrences, idf=idf))

    # Deterministic order: score first, then the term itself, so equal scores never shuffle
    # between runs (a lexicon that is stored and diffed has to be reproducible).
    ranked.sort(key=lambda t: (-t.score, t.term.lower()))
    return drop_subsumed_terms(ranked, subsumption_ratio)[:max_terms]


def lexicon_term_strings(terms: Iterable[LexiconTerm]) -> List[str]:
    """Just the term strings, ranking order preserved."""
    return [t.term for t in terms]


def terms_within_budget(terms: Iterable[str], max_terms: int, max_chars: int,
                        separator: str = ", ") -> List[str]:
    """Greedily pack `terms` into both caps, in the order given.

    Ranked input in, so the best terms are taken first and the tail is what gets dropped.
    A term too long for the remaining room is skipped rather than ending the packing, so
    one overlong phrase does not forfeit every shorter term behind it. De-duplication is
    case-insensitive, which matters when several sources are concatenated.
    """
    chosen: List[str] = []
    seen: Set[str] = set()
    used = 0
    for term in terms:
        key = term.lower().strip()
        if not key or key in seen:
            continue
        cost = len(term) + (len(separator) if chosen else 0)
        if used + cost > max_chars:
            continue
        seen.add(key)
        chosen.append(term)
        used += cost
        if len(chosen) >= max_terms:
            break
    return chosen
