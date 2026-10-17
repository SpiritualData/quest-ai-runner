"""The locate-a-file function. Every solution is ONE function with this signature:

    locate(case: dict) -> {"answer": str, "turns": int | None}

``case`` is one of seven "which file does X" tasks about this repository (ported from the retired standalone A/B script);
``answer`` is whatever text names the file(s) the solution points at, ``turns`` the tool-call rounds it used (None when
it does not run an agent). Scored by whether the labelled target path appears in ``answer``.

Arms:

* ``idf_hint`` (offline, recorded): what the WARM arm's context hint would contain, built deterministically (the
  zero-model IDF cards of ``context_selection``, top 3 files). Measures the hint, not Claude Code.
* ``cold_claude`` and ``warm_claude`` (live, need the claude CLI and spend haiku tokens; waived in waivers.csv S10):
  Claude Code alone, and Claude Code given the hint (or, for the adversarial case, a deliberately WRONG hint that
  must not mislead it). Their CLI runner, prompts and repo copy are ``live_support.py`` in this folder.

Correction to the original script: it bootstrapped a ``FileContextStore`` with no model provider, which writes zero
cards (``bootstrap skipped: no model provider wired``), so every warm hint came back empty and fell back to the
KNOWN-CORRECT target. Its warm arm was therefore told the answer. ``warm_claude`` here takes its hint from the
offline ``idf_hint`` solution instead.
"""
