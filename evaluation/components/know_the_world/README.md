# know_the_world (metric-know-the-world)

First measure for `know-the-world`: a claim about the world is stated only with a retrieved source that holds it. Nine questions over
the fixture pages of `web_search/cases.json` (loaded by id): six where a retrieved page supports the claim (the answer must state it and cite
that page), three where none does (no results, a failed search, a claim no snippet holds: the answer must say it found no source and
state nothing). Offline: `esoi run know_the_world --impl web_with_support_check`.

Arms: `web_with_support_check` (the production `WebResearchAdapter` plus a rule that composes the answer from the observation),
`memory_only` (floor), `broken/cite_first_result`. The answer is composed by rule, so how a real model writes from the same
observation, confidence calibration, live relevance and evidence strength are not measured; `research_sources_first` and
`evidence_weighted` are not built.
