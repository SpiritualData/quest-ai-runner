"""FastEditRunner — one model call, applied in process, through the write boundary.

Covers the two wire formats and, more importantly, the ways an edit can go WRONG:

  * whole-file rewrite (short files): the apply step is a write, so it cannot fail to apply;
  * SEARCH/REPLACE (longer files): exact match, the uniform-indent near-miss the vendored Aider
    matcher exists to absorb, and a genuine no-match that must leave the file BYTE-FOR-BYTE
    unchanged rather than half-applied;
  * the allow-list: an edit naming a file that was not in this turn's context is refused, so the
    model cannot widen its own blast radius;
  * declining: no candidate file, or nothing to change, returns met=False so the orchestrator's
    ladder escalates rather than the runner inventing work.

Fully offline: the provider is a scripted stub, and every write goes to tmp_path.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest

from quest_ai_runner.adapters.fast_edit_runner import FastEditConfig, FastEditRunner
from quest_ai_runner.adapters.files_writer import FilesWriter

SHORT_DOC = """# Project notes

Status: paused pending review.

Owner: the platform team.
"""

# Longer than FastEditConfig.whole_file_max_lines, so the runner picks SEARCH/REPLACE for it.
LONG_CODE = ("\n".join(f"# filler line {i}" for i in range(1, 500))
             + """

def retry(attempts):
    for i in range(attempts):
        wait = 1
        sleep(wait)
""")


class ScriptedProvider:
    """A ModelProvider whose ``answer`` replays scripted replies and records what it was asked."""

    def __init__(self, replies: List[str]):
        self.replies = list(replies)
        self.prompts: List[str] = []
        self.systems: List[Optional[str]] = []
        self.models: List[str] = []

    def plan(self, prompt: str, *, model: str, tool_schema: Dict[str, Any]) -> Dict[str, Any]:
        raise AssertionError("the fast edit path must not call plan()")

    def answer(self, messages, *, model, system=None, layers=None) -> str:
        self.prompts.append("\n".join(m["content"] for m in messages))
        self.systems.append(system)
        self.models.append(model)
        return self.replies.pop(0) if self.replies else ""

    def list_models(self) -> List[str]:
        return ["claude-sonnet-4-6", "claude-opus-4-8"]


@pytest.fixture
def corpus(tmp_path):
    root = tmp_path / "corpus"
    (root / "docs").mkdir(parents=True)
    (root / "docs" / "notes.md").write_text(SHORT_DOC)
    (root / "app.py").write_text(LONG_CODE)
    return root


def make_runner(corpus, tmp_path, replies, **cfg_kwargs):
    provider = ScriptedProvider(replies)
    writer = FilesWriter(str(corpus), backup_dir=str(tmp_path / "backups"))
    runner = FastEditRunner(provider=provider, writer=writer,
                            config=FastEditConfig(**cfg_kwargs))
    return runner, provider, writer


# --- whole-file rewrite -------------------------------------------------------------------------

def test_whole_file_rewrite_applies(corpus, tmp_path):
    new_doc = SHORT_DOC.replace("paused pending review", "active")
    runner, provider, _ = make_runner(corpus, tmp_path, [f"docs/notes.md\n```\n{new_doc}```\n"])

    res = runner.run_goal(goal="Update docs/notes.md so the status reads active",
                          brief="the status line is stale")

    assert res.met is True
    assert (corpus / "docs" / "notes.md").read_text() == new_doc
    assert "docs/notes.md" in res.output
    # It knows what it changed structurally, so future context costs it nothing.
    assert "docs/notes.md" in res.future_context


def test_short_file_uses_the_whole_file_format(corpus, tmp_path):
    runner, provider, _ = make_runner(corpus, tmp_path, ["NO_EDIT"])
    runner.run_goal(goal="touch docs/notes.md", brief="")
    assert "COMPLETE new content" in (provider.systems[0] or "")


def test_long_file_uses_search_replace(corpus, tmp_path):
    runner, provider, _ = make_runner(corpus, tmp_path, ["NO_EDIT"])
    runner.run_goal(goal="change the retry backoff in app.py", brief="")
    assert "<<<<<<< SEARCH" in (provider.systems[0] or "")


def test_whole_file_never_touches_a_file_it_was_not_given(corpus, tmp_path):
    other = corpus / "docs" / "other.md"
    other.write_text("untouched\n")
    reply = "docs/other.md\n```\nHIJACKED\n```\n"
    runner, _, _ = make_runner(corpus, tmp_path, [reply], max_retries=0)

    res = runner.run_goal(goal="Update docs/notes.md", brief="")

    assert res.met is False
    assert other.read_text() == "untouched\n"
    assert "not one of the files provided" in res.output


# --- SEARCH/REPLACE -----------------------------------------------------------------------------

def test_search_replace_exact_match_applies(corpus, tmp_path):
    reply = """app.py
```
<<<<<<< SEARCH
        wait = 1
=======
        wait = 2 ** i
>>>>>>> REPLACE
```
"""
    runner, _, _ = make_runner(corpus, tmp_path, [reply])

    res = runner.run_goal(goal="make the retry backoff in app.py exponential", brief="")

    assert res.met is True
    assert "wait = 2 ** i" in (corpus / "app.py").read_text()


def test_search_replace_absorbs_a_uniform_indent_near_miss(corpus, tmp_path):
    """The single most common way a model gets SEARCH wrong: right lines, wrong indent, applied
    uniformly. Upstream Aider handles it, which is most of why the matcher is vendored rather than
    reimplemented as "exact match only"."""
    reply = """app.py
```
<<<<<<< SEARCH
wait = 1
sleep(wait)
=======
wait = 2 ** i
sleep(wait)
>>>>>>> REPLACE
```
"""
    runner, _, _ = make_runner(corpus, tmp_path, [reply])

    res = runner.run_goal(goal="make the retry backoff in app.py exponential", brief="")

    assert res.met is True
    text = (corpus / "app.py").read_text()
    # The replacement is re-indented to the indent that was actually found in the file.
    assert "        wait = 2 ** i\n        sleep(wait)\n" in text


def test_a_genuine_no_match_leaves_the_file_untouched(corpus, tmp_path):
    before = (corpus / "app.py").read_text()
    reply = """app.py
```
<<<<<<< SEARCH
        wait = compute_backoff(i, jitter=True)
=======
        wait = 2 ** i
>>>>>>> REPLACE
```
"""
    runner, _, _ = make_runner(corpus, tmp_path, [reply], max_retries=0)

    res = runner.run_goal(goal="make the retry backoff in app.py exponential", brief="")

    assert res.met is False
    assert (corpus / "app.py").read_text() == before, "a failed match must never write anything"
    assert "SearchReplaceNoExactMatch" in res.output


def test_a_no_match_report_names_the_lines_that_are_actually_there(corpus, tmp_path):
    reply = """app.py
```
<<<<<<< SEARCH
    for i in range(attempts):
        wait = 1.0
        sleep(wait)
=======
    for i in range(attempts):
        sleep(2 ** i)
>>>>>>> REPLACE
```
"""
    runner, _, _ = make_runner(corpus, tmp_path, [reply], max_retries=0)
    res = runner.run_goal(goal="fix the backoff in app.py", brief="")
    assert "Did you mean" in res.output
    assert "wait = 1" in res.output


def test_a_failed_block_abandons_that_file_rather_than_applying_half_of_it(corpus, tmp_path):
    """Two blocks against one file, the second unmatchable. A partially applied chain is the one
    outcome worse than no edit at all, so the whole file is left alone."""
    before = (corpus / "app.py").read_text()
    reply = """app.py
```
<<<<<<< SEARCH
        wait = 1
=======
        wait = 2 ** i
>>>>>>> REPLACE
```

app.py
```
<<<<<<< SEARCH
        raise NeverPresent()
=======
        pass
>>>>>>> REPLACE
```
"""
    runner, _, _ = make_runner(corpus, tmp_path, [reply], max_retries=0)

    res = runner.run_goal(goal="fix retry in app.py", brief="")

    assert res.met is False
    assert (corpus / "app.py").read_text() == before


def test_one_retry_is_fed_the_diagnostic_and_can_land(corpus, tmp_path):
    bad = """app.py
```
<<<<<<< SEARCH
        wait = compute_backoff(i)
=======
        wait = 2 ** i
>>>>>>> REPLACE
```
"""
    good = """app.py
```
<<<<<<< SEARCH
        wait = 1
=======
        wait = 2 ** i
>>>>>>> REPLACE
```
"""
    runner, provider, _ = make_runner(corpus, tmp_path, [bad, good], max_retries=1)

    res = runner.run_goal(goal="make the retry backoff in app.py exponential", brief="")

    assert res.met is True
    assert len(provider.prompts) == 2
    assert "SearchReplaceNoExactMatch" in provider.prompts[1]
    assert "wait = 2 ** i" in (corpus / "app.py").read_text()


def test_a_shell_command_block_is_refused(corpus, tmp_path):
    reply = "```bash\nrm -rf docs\n```\n"
    runner, _, _ = make_runner(corpus, tmp_path, [reply], max_retries=0)
    res = runner.run_goal(goal="clean up app.py", brief="")
    assert res.met is False
    assert "only edits files" in res.output
    assert (corpus / "docs").is_dir()


# --- declining ----------------------------------------------------------------------------------

def test_no_candidate_file_declines_without_calling_the_model(corpus, tmp_path):
    runner, provider, _ = make_runner(corpus, tmp_path, [])
    res = runner.run_goal(goal="rewrite the onboarding flow", brief="make it better")
    assert res.met is False
    assert provider.prompts == [], "nothing to edit means nothing to spend a model call on"
    assert "no candidate file" in (res.error or "")
    # Non-empty output on purpose: the goal loop treats an error with NO output as terminal, and
    # declining must escalate to the next rung instead of ending the goal.
    assert res.output.strip()


def test_a_path_outside_the_root_is_never_a_candidate(corpus, tmp_path):
    outside = tmp_path / "elsewhere.md"
    outside.write_text("secret\n")
    runner, provider, _ = make_runner(corpus, tmp_path, [])
    res = runner.run_goal(goal=f"update {outside}", brief="")
    assert res.met is False
    assert provider.prompts == []
    assert outside.read_text() == "secret\n"


def test_a_secretish_file_is_never_a_candidate(corpus, tmp_path):
    (corpus / ".env").write_text("KEY=value\n")
    runner, provider, _ = make_runner(corpus, tmp_path, [])
    res = runner.run_goal(goal="update the .env file with the new key", brief="")
    assert res.met is False
    assert provider.prompts == []
    assert (corpus / ".env").read_text() == "KEY=value\n"


def test_no_edit_sentinel_declines(corpus, tmp_path):
    runner, _, _ = make_runner(corpus, tmp_path, ["NO_EDIT"])
    res = runner.run_goal(goal="update docs/notes.md if anything is stale", brief="")
    assert res.met is False
    assert (corpus / "docs" / "notes.md").read_text() == SHORT_DOC


def test_a_provider_failure_is_reported_never_raised(corpus, tmp_path):
    class Boom(ScriptedProvider):
        def answer(self, messages, *, model, system=None, layers=None) -> str:
            raise RuntimeError("provider is down")

    writer = FilesWriter(str(corpus), backup_dir=str(tmp_path / "b"))
    runner = FastEditRunner(provider=Boom([]), writer=writer)
    res = runner.run_goal(goal="update docs/notes.md", brief="")
    assert res.met is False and "provider is down" in (res.error or "")


def test_the_prompt_carries_the_context_qar_already_gathered(corpus, tmp_path):
    runner, provider, _ = make_runner(corpus, tmp_path, ["NO_EDIT"])
    runner.run_goal(goal="update docs/notes.md", brief="the brief",
                    context_preamble="CARD: the review finished on Tuesday")
    assert "the review finished on Tuesday" in provider.prompts[0]
    # The whole point: the file content is already in the prompt, so the model needs no turn to
    # go and read it.
    assert "Status: paused pending review." in provider.prompts[0]


def test_it_uses_the_model_the_orchestrator_pinned_for_the_attempt(corpus, tmp_path):
    runner, provider, _ = make_runner(corpus, tmp_path, ["NO_EDIT"])
    runner.run_goal(goal="update docs/notes.md", brief="", model="claude-opus-4-8")
    assert provider.models == ["claude-opus-4-8"]


def test_it_resolves_a_tier_when_no_model_is_pinned(corpus, tmp_path):
    runner, provider, _ = make_runner(corpus, tmp_path, ["NO_EDIT"])
    runner.run_goal(goal="update docs/notes.md", brief="")
    assert provider.models[0] in provider.list_models()


def test_the_previous_content_is_backed_up_before_a_fast_edit(corpus, tmp_path):
    new_doc = SHORT_DOC.replace("paused pending review", "active")
    runner, _, _ = make_runner(corpus, tmp_path, [f"docs/notes.md\n```\n{new_doc}```\n"])
    runner.run_goal(goal="Update docs/notes.md so the status reads active", brief="")
    saved = list((tmp_path / "backups").iterdir())
    assert len(saved) == 1 and saved[0].read_text() == SHORT_DOC
