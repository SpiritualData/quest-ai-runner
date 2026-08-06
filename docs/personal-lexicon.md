# Personal lexicon

**Code:** `adapters/personal_lexicon.py` (word bands in `adapters/english_word_bands.py`).
**Cost:** none. Model-free and dependency-free arithmetic over text, safe to run on every write.

A ranked list of the terms one person actually uses: the words and multi-word phrases that recur in
their own writing and that are not simply common English or common to everybody else in the
deployment.

## The problem it solves

Several things a consumer wants to do need to know a single person's vocabulary, and none of them
can get it from a general language model:

- **Biasing a speech recognizer.** A recognizer given a short list of the terms this speaker
  actually says stops turning them into the nearest ordinary word. The list has a hard size budget,
  so it has to be ranked, not merely collected.
- **Query expansion and labelling.** The person's own words are the ones their own corpus is
  indexed by, so they are the right seeds for expanding a search or naming a cluster.
- **Grounding a persona.** How someone writes is partly which words they reach for.

The naive version of this (take the most frequent words) returns "the", "work" and "today" for
every person alive. The naive fix (drop a stopword list) returns whatever the deployment's own house
vocabulary happens to be, identically for every user. Both fail the same way: they measure volume,
and what is wanted is distinctiveness.

## How the ranking works

Each candidate term gets one score:

```
score(t) = (1 + ln TF) * (1 + ln DF) * IDF(t)

TF   total occurrences of the term across this person's documents
DF   how many of their documents contain it
IDF  how distinctive the term is OUTSIDE this person (from a background source)
```

**Both counts are log-damped on purpose.** Undamped, the product is dominated by whichever term was
typed most, so one long entry or one much-repeated ordinary word buries the rare term that is the
actual point of the exercise. Damping makes each further repetition worth less than the one before,
which leaves IDF, the only undamped factor, as the term that decides the ranking. That is the
intended shape: volume should break ties between distinctive terms, never outvote distinctiveness.

TF and DF are kept as separate factors rather than merged because they answer different questions.
TF says the person leans on the term. DF says they lean on it in more than one place, which is what
separates a habit of speech from a single entry that happened to be about one subject.

**Candidate terms** are word n-grams up to `max_phrase_words` (default 3) that neither start nor end
on a function word. Interior function words are kept, and that is deliberate: "law of attraction"
and "fear of failure" are exactly the phrases worth having, while "of attraction" and "the fear" are
not terms at all. The edge list is narrow by design, not a general stopword list, so content words
like "work" or "feel" stay eligible as term edges ("deep work", "feel stuck").

Casing is preserved. The surface form returned is the one the person wrote most often, because how
they capitalize a term is part of the spelling hint a consumer is going to pass on.

## The two background sources, and why the minimum

`IDF` comes from a `BackgroundFrequency`. Two implementations answer the same question from
different directions:

| Source | Question it answers | Available |
|---|---|---|
| `LanguageBaseline` | Is this word common in the language at large? | Immediately, day one, first user |
| `CorpusBackground` | Do all the other people in this deployment use it too? | Once the population is large enough |

`LanguageBaseline` reads the bundled three-band English frequency list. A word in the most common
band gets IDF 0, which collapses the whole score to zero, so ordinary English never reaches the
output and no separate stopword pass is needed at ranking time. Its blind spots are structural: it
knows only English, and it knows nothing of the deployment, so a product's own house vocabulary and
any non-English function word look maximally distinctive to it.

`CorpusBackground` is built from how many members of the population use each term, which a consumer
can accumulate while computing lexicons rather than by scanning anything extra. It corrects exactly
the blind spots above: a word every user writes scores 0 whatever language it is in. Below
`min_population` (default 25) members it **abstains** and returns `None` rather than a number,
because with five users a term two of them use looks rare by arithmetic and common by eye.

`CombinedBackground` takes the **lowest** IDF any source is willing to give, so a term has to look
distinctive on every available axis to rank. The minimum is the right combiner because each source
is authoritative about its own disqualification and ignorant of the other's: general English cannot
know your house words, and a young population cannot know English. Averaging would let a strong
opinion from an ignorant source rescue a term the informed source already disqualified. `None` means
"no opinion" and is skipped, never treated as agreement on zero, so an abstaining corpus source
leaves the language baseline answering alone rather than silently blanking the whole lexicon.

## The min_documents safeguard

`min_documents` defaults to 2, and that default matters most wherever this output feeds back into
its own input.

When the documents include dictated text, a one-off speech mis-recognition is a plausible-looking
rare term: it is absent from every English band, so it scores maximally distinctive, and repeating
it a few times inside the one entry it appeared in is enough volume to rank. Promote it into the
lexicon, hand the lexicon back to the recognizer as a bias hint, and the recognizer has been taught
to produce that same error again, more confidently, forever.

Requiring the term to appear in at least two independent documents is what breaks the loop. A slip
happens once. A person's real vocabulary recurs. `min_occurrences` (default 2) is the weaker
companion condition on raw volume; `min_documents` is the one carrying the safety property, and it
is covered by a named test.

## Usage

```python
from quest_ai_runner.adapters.personal_lexicon import (
    CombinedBackground, CorpusBackground, LanguageBaseline,
    build_personal_lexicon, lexicon_term_strings, terms_within_budget,
)

# One string per source item. Keep them separate: each is a document for DF purposes.
documents = load_this_persons_entries()

# Day one, with no population to compare against, the language baseline alone is fine.
background = LanguageBaseline()

# Once the deployment has enough users, add their aggregate opinion. term_counts maps a lowercased
# term to how many DISTINCT members use it; accumulate it as you compute each person's lexicon.
background = CombinedBackground(
    CorpusBackground(term_counts, population=member_count),
    LanguageBaseline(),
)

lexicon = build_personal_lexicon(documents, background=background, max_terms=120)
for term in lexicon[:5]:
    print(term.term, term.score, term.documents, term.occurrences)

# Pack the ranked terms into whatever budget the downstream API imposes.
hint = terms_within_budget(lexicon_term_strings(lexicon), max_terms=60, max_chars=800)
```

`LexiconTerm.as_dict()` / `from_dict()` round-trip a stored lexicon. The ranking is fully
deterministic, including the order of equal-scoring terms, so a stored lexicon can be diffed between
runs and the diff means something.

`drop_subsumed_terms` runs as part of `build_personal_lexicon` and retires a shorter term that a
better-ranked term already contains and accounts for, so a hard budget is not spent three times on
"law", "attraction" and "law of attraction". A word the person genuinely also uses on its own keeps
its slot (controlled by `subsumption_ratio`).

## Limitations

Known and deliberate, listed so a consumer can decide whether they matter for its use:

- **The bundled bands are English only.** For a document in another language, the language baseline
  rates ordinary function words as maximally distinctive. Only a minimum-word-length rule holds this
  back, so short non-English function words can reach the output. `CorpusBackground` is the real fix
  and works in any language, but it needs a population.
- **No stemming or lemmatization.** "practice", "practices" and "practicing" are three terms
  competing for three budget slots. This is a real cost, accepted because stemming is
  language-specific, would need a dependency, and would destroy the exact surface form that makes
  the output usable as a spelling hint.
- **No recency weighting.** A term the person used constantly two years ago and never since ranks
  the same as one they picked up last week. A consumer that cares about drift should slice the
  documents it passes in by time rather than expect the ranking to do it.
- **No semantic grouping.** Two different spellings, an abbreviation and its expansion, or a term
  and its synonym are unrelated as far as this is concerned.
- **Digits are dropped entirely.** A year or a version number is not vocabulary and cannot be used
  as a spelling hint, so the tokenizer never emits one.

## Tests

`tests/test_personal_lexicon.py` covers the properties the design rests on: distinctiveness beating
equal volume, common-band words collapsing to nothing, phrase formation and the edge rule, the
`min_documents` feedback-loop safeguard, dominant casing, determinism and stable tie order, both
background sources plus abstention and the minimum combiner, subsumption, budget packing, and that
degenerate and non-English input returns something sane rather than raising.
