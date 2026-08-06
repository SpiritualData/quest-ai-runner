"""Offline tests for the personal lexicon (adapters/personal_lexicon.py).

The property being guarded: what comes back is the vocabulary that is *this person's*, not the
vocabulary they happen to write most. Volume is damped, ordinary English collapses to nothing, and
a term nobody else in the deployment avoids is not personal to anyone. Pure arithmetic over text:
no model, no network, no fixtures.
"""
from quest_ai_runner.adapters.personal_lexicon import (
    CombinedBackground,
    CorpusBackground,
    LanguageBaseline,
    LexiconTerm,
    build_personal_lexicon,
    drop_subsumed_terms,
    lexicon_term_strings,
    terms_within_budget,
    tokenize,
)


# ---------------------------------------------------------------------------
# Ranking: distinctiveness beats volume
# ---------------------------------------------------------------------------

def test_distinctive_term_outranks_an_equally_used_common_word():
    # "kundalini" is in no English band; "budget" is ordinary vocabulary. Both are written by this
    # person about as often, so only distinctiveness can separate them -- which is the whole point.
    documents = [
        "The kundalini practice went deep today. My budget felt tight but the kundalini work held.",
        "Again kundalini rising in the morning. I reviewed the budget and the numbers were fine.",
        "kundalini and budget both on my mind; kundalini more so. budget review later.",
    ]
    ranked = build_personal_lexicon(documents)
    terms = lexicon_term_strings(ranked)

    assert terms[0] == "kundalini"
    assert "budget" in terms
    by_term = {t.term: t for t in ranked}
    assert by_term["kundalini"].score > by_term["budget"].score
    assert by_term["kundalini"].idf > by_term["budget"].idf


def test_most_common_band_words_never_reach_the_output():
    # A BAND_COMMON word has IDF 0, which collapses the whole score, so it drops out with no
    # separate stopword pass. "work" is the case that matters: it is not a term EDGE word, so it
    # reaches scoring as a candidate and is rejected on IDF alone.
    documents = [
        "The work and the kundalini work: the work is the work, and it is good.",
        "Work again, and the kundalini work carried the day. The work and the rest.",
    ]
    terms = lexicon_term_strings(build_personal_lexicon(documents))

    assert "kundalini" in terms
    for common in ("the", "and", "work", "Work", "it", "is"):
        assert common not in terms


# ---------------------------------------------------------------------------
# Term shape: phrases, and where a term may not begin or end
# ---------------------------------------------------------------------------

def test_phrase_straddling_an_interior_function_word_is_one_term():
    documents = [
        "I keep coming back to the law of attraction and what it means.",
        "The law of attraction showed up again today in my reading.",
        "law of attraction, always the law of attraction.",
    ]
    terms = lexicon_term_strings(build_personal_lexicon(documents))
    assert "law of attraction" in terms


def test_no_term_starts_or_ends_on_a_function_word():
    documents = [
        "The fear of failure is the fear I keep meeting in the law of attraction work.",
        "Again the fear of failure, and again the law of attraction under it.",
    ]
    terms = lexicon_term_strings(build_personal_lexicon(documents))

    for bad in ("of attraction", "the fear", "of failure", "the law", "attraction and"):
        assert bad not in terms
    for term in terms:
        words = term.lower().split()
        assert words[0] not in ("the", "of", "and", "a", "in")
        assert words[-1] not in ("the", "of", "and", "a", "in")


# ---------------------------------------------------------------------------
# min_documents: the feedback-loop safeguard
# ---------------------------------------------------------------------------

def test_min_documents_keeps_a_one_off_mis_recognition_out_of_the_lexicon():
    # PROTECTS: this lexicon can be fed back to the speech recognizer that produced its own input.
    # A single dictation slip repeated inside ONE entry looks exactly like a rare personal term by
    # frequency alone, and promoting it would bias the recognizer to produce the same slip again.
    # Corroboration from a second, independent document is what breaks that loop.
    dictated = [
        "Dictating tonight: flurbdazzle flurbdazzle flurbdazzle kept coming up while I sat "
        "with kundalini.",
        "Back to kundalini this evening, steady and slow.",
    ]
    terms = lexicon_term_strings(build_personal_lexicon(dictated))
    assert "flurbdazzle" not in terms   # three occurrences, but all in one document
    assert "kundalini" in terms         # twice, across two documents

    # The same word, once each in two separate documents, is real vocabulary and is admitted.
    corroborated = [
        "Dictating tonight: flurbdazzle kept coming up while I sat with kundalini.",
        "flurbdazzle again, and back to kundalini this evening.",
    ]
    assert "flurbdazzle" in lexicon_term_strings(build_personal_lexicon(corroborated))


# ---------------------------------------------------------------------------
# Surface form and determinism
# ---------------------------------------------------------------------------

def test_the_persons_own_dominant_capitalization_comes_back():
    documents = [
        "Kundalini practice today. Kundalini again this afternoon.",
        "Kundalini in the evening, plus a few kundalini notes.",
    ]
    terms = lexicon_term_strings(build_personal_lexicon(documents))
    assert "Kundalini" in terms
    assert "kundalini" not in terms


def test_same_input_gives_the_same_output_and_equal_scores_have_a_stable_order():
    documents = [
        "quorbizzle and flurbdazzle showed up today in the notes.",
        "flurbdazzle again, quorbizzle again, same as before.",
    ]
    first = build_personal_lexicon(documents)
    second = build_personal_lexicon(documents)
    assert [t.as_dict() for t in first] == [t.as_dict() for t in second]

    by_term = {t.term: t for t in first}
    assert by_term["flurbdazzle"].score == by_term["quorbizzle"].score
    # Equal scores break on the term itself, so a stored lexicon diffs cleanly between runs.
    order = [t.term for t in first]
    assert order.index("flurbdazzle") < order.index("quorbizzle")


# ---------------------------------------------------------------------------
# Background sources: language bands, the population, and combining them
# ---------------------------------------------------------------------------

def test_a_term_everybody_uses_drops_out_even_though_english_calls_it_rare():
    # "quest" is absent from the English bands, so the language baseline rates it maximally
    # distinctive. Every member of the population writes it, so the corpus background rates it at
    # zero -- and a house word is personal to nobody.
    population = 400
    corpus = CorpusBackground({"quest": population, "kundalini": 3}, population=population)
    assert corpus.idf("quest") == 0.0
    assert corpus.idf("kundalini") > 1.0
    assert LanguageBaseline().idf("quest") > 1.0

    documents = [
        "The quest kundalini writing today.",
        "quest again, kundalini again.",
    ]
    background = CombinedBackground(corpus, LanguageBaseline())
    terms = lexicon_term_strings(build_personal_lexicon(documents, background=background))
    assert terms == ["kundalini"]
    # Without the population's opinion, the house word looks just as personal as the real one.
    assert "quest" in lexicon_term_strings(build_personal_lexicon(documents))


def test_corpus_background_abstains_below_min_population_and_the_language_baseline_answers():
    thin = CorpusBackground({"quest": 4}, population=5)
    assert thin.idf("quest") is None   # five users cannot tell rare from common

    # None is "no opinion", not "no distinctiveness": the combination must fall through to the
    # language baseline rather than treating the abstention as agreement on zero.
    combined = CombinedBackground(thin, LanguageBaseline())
    assert combined.idf("quest") == LanguageBaseline().idf("quest")
    assert combined.idf("quest") > 0

    documents = ["The quest writing today, quest notes.", "quest again this evening."]
    assert "quest" in lexicon_term_strings(build_personal_lexicon(documents, background=combined))


def test_combined_background_takes_the_minimum_across_sources():
    population = 400
    corpus = CorpusBackground({"quest": population}, population=population)
    combined = CombinedBackground(corpus, LanguageBaseline())
    assert combined.idf("quest") == min(corpus.idf("quest"), LanguageBaseline().idf("quest"))
    assert combined.idf("quest") == 0.0

    # With no source willing to answer at all, the combination has no opinion either.
    assert CombinedBackground(CorpusBackground({}, population=1)).idf("quest") is None


# ---------------------------------------------------------------------------
# Budget helpers
# ---------------------------------------------------------------------------

def test_subsumed_word_is_dropped_but_a_word_used_on_its_own_is_kept():
    phrase = LexiconTerm(term="law of attraction", score=10.0, documents=3, occurrences=4, idf=2.1)
    inside_only = LexiconTerm(term="attraction", score=5.0, documents=3, occurrences=4, idf=3.0)
    also_alone = LexiconTerm(term="law", score=4.0, documents=3, occurrences=9, idf=0.8)

    kept = [t.term for t in drop_subsumed_terms([phrase, inside_only, also_alone])]
    assert kept == ["law of attraction", "law"]


def test_terms_within_budget_respects_both_caps_dedupes_and_skips_an_overlong_term():
    assert terms_within_budget(["alpha", "beta", "gamma"], max_terms=2, max_chars=100) == \
        ["alpha", "beta"]
    assert terms_within_budget(["alpha", "ALPHA", "beta"], max_terms=5, max_chars=100) == \
        ["alpha", "beta"]
    # The overlong first term is skipped, and the shorter terms behind it still get their slots.
    assert terms_within_budget(["a very extremely long overlong phrase", "beta", "gamma"],
                               max_terms=5, max_chars=20) == ["beta", "gamma"]
    assert terms_within_budget(["ab", "", "   ", "cd"], max_terms=5, max_chars=100) == ["ab", "cd"]


# ---------------------------------------------------------------------------
# Degenerate and non-English input
# ---------------------------------------------------------------------------

def test_empty_and_unusable_input_returns_nothing_without_raising():
    assert build_personal_lexicon([]) == []
    assert build_personal_lexicon(["", "   \n\t "]) == []
    assert build_personal_lexicon(["the and of", "the and of"]) == []   # nothing admissible
    assert build_personal_lexicon(["123 456", "789"]) == []             # digits are not vocabulary


def test_non_english_text_produces_sane_tokens_without_raising():
    # Documenting current behavior, not claiming it is right: the edge-word list is English, so a
    # Spanish function word can still open a term. Accented letters must survive tokenization, and
    # nothing may raise. CorpusBackground is the real fix for the function-word case.
    documents = [
        "La meditación de la mañana me dejó tranquilo y con energía.",
        "Otra vez la meditación por la mañana, con mucha energía.",
    ]
    assert "meditación" in tokenize(documents[0])

    terms = lexicon_term_strings(build_personal_lexicon(documents))
    assert terms
    assert all(term.strip() == term and term for term in terms)
    assert any("energía" in term for term in terms)
