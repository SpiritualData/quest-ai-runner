"""The attended half of a quest folder's standing next-steps artifact.

A person opened a fresh `qar chat` in a folder whose QUEST_SYNC.md already carried a
`QAR:MANAGED:next_steps` block, asked what to do next, and watched the AI re-derive an answer from
goals, notes and files. The artifact was only reachable through ordinary corpus retrieval, competing
with every other file on relevance, so the standing answer was not treated as standing at all.

These cover `runner/session_next_steps.py` (read it at session start, render it as authoritative
per-turn context, and refresh it from a turn that warrants it) plus `quest_id_in_folder`, all
offline: a temp folder with a real sync file and a fake Quest client. Domain-free per hard rule #1
— fictional quest ids and placeholder work only.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import quest_ai_runner
from quest_ai_runner.interactive_session import InteractiveSession
from quest_ai_runner.runner.quest_folder_sync import (
    NEXT_STEPS_ENTRY_NAME,
    quest_id_in_folder,
    read_next_steps,
)
from quest_ai_runner.runner import session_next_steps as sns


QUEST_ID = "quest_aaaa1111bbbb"

SYNC_FILE = """---
quest_id: quest_aaaa1111bbbb
---

<!-- QAR:MANAGED:goal START -->
**Goal:** Ship the first public release
**Status:** In progress
<!-- QAR:MANAGED:goal END -->

<!-- QAR:MANAGED:next_steps START -->
## Next steps

_Refreshed 2026-08-12, by the autopilot pass, for day:2026-08-12. This block is replaced on every \
refresh, so it is the current answer rather than a log._

1. Finish the packaging checklist (target 2026-08-15)
2. Draft the announcement post

Carrying over, not finished in the previous period:
- Decide on the licence header
<!-- QAR:MANAGED:next_steps END -->

<!-- QAR:MANAGED:notes START -->
## Notes from Quest

- <!-- id:note_1 --> [2026-08-11] (You) packaging is nearly done
<!-- QAR:MANAGED:notes END -->

## Notes to push to Quest

- a local finding not yet posted
"""


def _folder(tmp_path, contents=SYNC_FILE, name="QUEST_SYNC.md"):
    (tmp_path / name).write_text(contents, encoding="utf-8")
    return str(tmp_path)


def _cfg(corpus_root=None, quest_folder_map=None):
    return SimpleNamespace(corpus_root=corpus_root, quest_folder_map=quest_folder_map)


def _deep(met=True, deferred=False, output="did the thing"):
    return SimpleNamespace(met=met, deferred=deferred, output=output)


class _FakeQuestClient:
    """Context-entry upsert surface only, which is the path publish_next_steps prefers."""

    def __init__(self, entries=None, fail_on=None):
        self.entries = list(entries or [])
        self.fail_on = fail_on or set()
        self.calls = []

    def list_context_entries(self, quest_id):
        self.calls.append(("list", quest_id))
        if "list" in self.fail_on:
            raise RuntimeError("quest api is down")
        return list(self.entries)

    def create_context_entry(self, quest_id, name, content):
        self.calls.append(("create", quest_id, name))
        if "create" in self.fail_on:
            raise RuntimeError("quest api is down")
        entry = {"id": "entry_1", "name": name, "content": content}
        self.entries.append(entry)
        return entry

    def update_context_entry(self, quest_id, entry_id, content=""):
        self.calls.append(("update", quest_id, entry_id))
        if "update" in self.fail_on:
            raise RuntimeError("quest api is down")
        return {"id": entry_id, "content": content}


# --- quest_id_in_folder -------------------------------------------------------

def test_quest_id_comes_from_the_sync_file_frontmatter(tmp_path):
    assert quest_id_in_folder(_folder(tmp_path)) == QUEST_ID


def test_quest_id_is_none_without_a_sync_file(tmp_path):
    assert quest_id_in_folder(str(tmp_path)) is None


def test_quest_id_is_none_when_the_file_has_no_frontmatter(tmp_path):
    assert quest_id_in_folder(_folder(tmp_path, "# just a heading\n\nno frontmatter here\n")) is None


def test_quest_id_ignores_a_quest_id_line_outside_the_frontmatter(tmp_path):
    """Only the frontmatter is authoritative; prose mentioning the key must not be mistaken for it."""
    body = "---\ntitle: no id here\n---\n\nquest_id: quest_not_the_real_one\n"
    assert quest_id_in_folder(_folder(tmp_path, body)) is None


# --- resolve_quest_folder -----------------------------------------------------

def test_folder_map_answers_first_and_returns_the_mapped_root(tmp_path):
    """A session started in a SUBFOLDER still resolves to the quest's mapped root, where the
    artifact lives."""
    root = tmp_path / "quest_root"
    (root / "sub").mkdir(parents=True)
    _folder(root)
    cfg = _cfg(corpus_root=str(root / "sub"), quest_folder_map={QUEST_ID: str(root)})
    quest_id, folder = sns.resolve_quest_folder(cfg)
    assert quest_id == QUEST_ID
    assert folder == str(root)


def test_frontmatter_is_the_fallback_when_no_folder_map_is_configured(tmp_path):
    folder = _folder(tmp_path)
    quest_id, resolved = sns.resolve_quest_folder(_cfg(corpus_root=folder))
    assert (quest_id, resolved) == (QUEST_ID, folder)


def test_unmapped_folder_with_no_frontmatter_resolves_no_quest_id(tmp_path):
    quest_id, folder = sns.resolve_quest_folder(_cfg(corpus_root=str(tmp_path)))
    assert quest_id == ""
    assert folder == str(tmp_path)


# --- load_standing_next_steps -------------------------------------------------

def test_session_start_reads_the_next_steps_block(tmp_path):
    standing = sns.load_standing_next_steps(_cfg(corpus_root=_folder(tmp_path)))
    assert standing is not None
    assert standing.quest_id == QUEST_ID
    assert "Finish the packaging checklist" in standing.text
    assert "Refreshed 2026-08-12, by the autopilot pass" in standing.text
    # Only the managed block, not the rest of the file.
    assert "Notes to push to Quest" not in standing.text
    assert standing.can_refresh()


def test_no_sync_file_degrades_to_nothing(tmp_path):
    assert sns.load_standing_next_steps(_cfg(corpus_root=str(tmp_path))) is None


def test_sync_file_without_a_next_steps_block_degrades_to_nothing(tmp_path):
    body = "---\nquest_id: quest_aaaa1111bbbb\n---\n\n<!-- QAR:MANAGED:goal START -->\n**Goal:** x\n<!-- QAR:MANAGED:goal END -->\n"
    assert sns.load_standing_next_steps(_cfg(corpus_root=_folder(tmp_path, body))) is None


def test_empty_next_steps_block_degrades_to_nothing(tmp_path):
    body = ("---\nquest_id: quest_aaaa1111bbbb\n---\n\n"
            "<!-- QAR:MANAGED:next_steps START -->\n   \n<!-- QAR:MANAGED:next_steps END -->\n")
    assert sns.load_standing_next_steps(_cfg(corpus_root=_folder(tmp_path, body))) is None


def test_an_unmapped_folder_still_contributes_its_artifact(tmp_path):
    """No quest id (nothing to publish under) must not cost the session the READ."""
    body = SYNC_FILE.replace("---\nquest_id: quest_aaaa1111bbbb\n---\n\n", "")
    standing = sns.load_standing_next_steps(_cfg(corpus_root=_folder(tmp_path, body)))
    assert standing is not None
    assert standing.quest_id == ""
    assert not standing.can_refresh()
    assert "Draft the announcement post" in standing.text


# --- render_standing_next_steps ----------------------------------------------

def test_rendered_block_labels_the_artifact_as_the_standing_answer(tmp_path):
    standing = sns.load_standing_next_steps(_cfg(corpus_root=_folder(tmp_path)))
    block = sns.render_standing_next_steps(standing)
    assert "STANDING NEXT-STEPS ARTIFACT" in block
    assert "QUEST_SYNC.md" in block
    assert "Finish the packaging checklist" in block
    # The freshness stamp travels inside the artifact itself, so the label never restates it.
    assert "Refreshed 2026-08-12" in block


def test_no_artifact_renders_nothing():
    assert sns.render_standing_next_steps(None) == ""


def test_a_long_hand_edited_block_is_capped(tmp_path):
    long_body = "\n".join(f"{i}. step number {i}" for i in range(2000))
    body = ("---\nquest_id: quest_aaaa1111bbbb\n---\n\n"
            f"<!-- QAR:MANAGED:next_steps START -->\n{long_body}\n<!-- QAR:MANAGED:next_steps END -->\n")
    standing = sns.load_standing_next_steps(_cfg(corpus_root=_folder(tmp_path, body)))
    block = sns.render_standing_next_steps(standing)
    assert len(block) < len(long_body)
    assert "truncated" in block


# --- next_steps_from_turn (the refresh trigger) -------------------------------

def test_a_turn_with_no_execution_does_not_refresh():
    """Small talk, a question, an ordinary answer: nothing executed, so nothing about what is
    next changed."""
    assert sns.next_steps_from_turn([], [], updated="2026-08-14") is None
    assert sns.next_steps_from_turn(["do the thing"], [], updated="2026-08-14") is None


def test_a_fully_finished_turn_leaves_the_standing_answer_alone():
    """It knows what it COMPLETED, not what comes after; an empty block would be worse than a
    stale considered one."""
    assert sns.next_steps_from_turn(["a", "b"], [_deep(met=True), _deep(met=True)]) is None


def test_an_unfinished_goal_becomes_the_new_standing_answer():
    ns = sns.next_steps_from_turn(["write the docs", "cut the release"],
                                  [_deep(met=True), _deep(met=False)],
                                  updated="2026-08-14")
    assert ns is not None
    assert ns.steps == ["cut the release"]
    assert ns.source == sns.ATTENDED_SOURCE
    assert ns.updated == "2026-08-14"
    assert "1 finished cleanly" in ns.note


def test_a_deferred_handoff_counts_as_unfinished():
    """Queued out of band is not done: reporting it complete would claim an outcome nobody saw."""
    ns = sns.next_steps_from_turn(["migrate the store"], [_deep(met=True, deferred=True)])
    assert ns is not None
    assert ns.steps == ["migrate the store"]


def test_unalignable_results_are_all_or_nothing():
    """A sequential-group deep run records results without a matching goal entry, so per-goal
    attribution is impossible and guessing would be worse than listing everything."""
    partial = sns.next_steps_from_turn(["a", "b"], [_deep(met=True), _deep(met=False), _deep(met=False)])
    assert partial is not None and partial.steps == ["a", "b"]
    finished = sns.next_steps_from_turn(["a", "b"], [_deep(met=True), _deep(met=True), _deep(met=True)])
    assert finished is None


# --- refresh_from_turn --------------------------------------------------------

def test_refresh_rewrites_the_block_locally_and_upserts_it_on_quest(tmp_path):
    folder = _folder(tmp_path)
    standing = sns.load_standing_next_steps(_cfg(corpus_root=folder))
    client = _FakeQuestClient()
    result = sns.refresh_from_turn(client, standing, goals=["cut the release"],
                                   deep_results=[_deep(met=False)], updated="2026-08-14")
    assert result is not None and result.quest_target == "context_entry"
    on_disk = read_next_steps(folder)
    assert "cut the release" in on_disk
    # A REPLACE, not a log: the answer it superseded is gone, and the rest of the file survives.
    assert "Finish the packaging checklist" not in on_disk
    assert "Notes to push to Quest" in (tmp_path / "QUEST_SYNC.md").read_text()
    # The session now asserts what it just wrote, not the answer it replaced.
    assert "cut the release" in standing.text
    assert ("create", QUEST_ID, NEXT_STEPS_ENTRY_NAME) in client.calls


def test_refresh_is_skipped_when_the_turn_does_not_warrant_one(tmp_path):
    folder = _folder(tmp_path)
    standing = sns.load_standing_next_steps(_cfg(corpus_root=folder))
    client = _FakeQuestClient()
    assert sns.refresh_from_turn(client, standing, goals=["a"], deep_results=[]) is None
    assert client.calls == []
    assert "Finish the packaging checklist" in read_next_steps(folder)


def test_refresh_is_skipped_when_no_quest_id_resolved(tmp_path):
    body = SYNC_FILE.replace("---\nquest_id: quest_aaaa1111bbbb\n---\n\n", "")
    folder = _folder(tmp_path, body)
    standing = sns.load_standing_next_steps(_cfg(corpus_root=folder))
    client = _FakeQuestClient()
    assert sns.refresh_from_turn(client, standing, goals=["a"],
                                 deep_results=[_deep(met=False)]) is None
    assert client.calls == []
    assert "Finish the packaging checklist" in read_next_steps(folder)


def test_no_artifact_at_all_refreshes_nothing():
    assert sns.refresh_from_turn(_FakeQuestClient(), None, goals=["a"],
                                 deep_results=[_deep(met=False)]) is None


def test_a_quest_api_failure_still_leaves_a_correct_local_artifact(tmp_path):
    """The local copy is the one the person works next to, so a Quest outage must not cost it."""
    folder = _folder(tmp_path)
    standing = sns.load_standing_next_steps(_cfg(corpus_root=folder))
    result = sns.refresh_from_turn(_FakeQuestClient(fail_on={"list"}), standing,
                                   goals=["cut the release"], deep_results=[_deep(met=False)],
                                   updated="2026-08-14")
    assert result is not None
    assert result.quest_target == "none" and result.detail
    assert "cut the release" in read_next_steps(folder)


def test_no_quest_client_at_all_still_refreshes_the_local_file(tmp_path):
    folder = _folder(tmp_path)
    standing = sns.load_standing_next_steps(_cfg(corpus_root=folder))
    result = sns.refresh_from_turn(None, standing, goals=["cut the release"],
                                   deep_results=[_deep(met=False)], updated="2026-08-14")
    assert result is not None and result.quest_target == "none"
    assert "cut the release" in read_next_steps(folder)


def test_a_cancelled_or_answer_turn_never_refreshes(tmp_path):
    """The kind comes from the orchestrator's own structured result, never from its prose."""
    folder = _folder(tmp_path)
    standing = sns.load_standing_next_steps(_cfg(corpus_root=folder))
    client = _FakeQuestClient()
    for kind in ("answer", "cancelled", "confirm", ""):
        assert sns.refresh_from_turn(client, standing, kind=kind, goals=["cut the release"],
                                     deep_results=[_deep(met=False)]) is None
    assert client.calls == []
    assert "Finish the packaging checklist" in read_next_steps(folder)


def test_a_failing_write_never_raises_into_the_turn(tmp_path, monkeypatch):
    folder = _folder(tmp_path)
    standing = sns.load_standing_next_steps(_cfg(corpus_root=folder))

    def _boom(*_a, **_kw):
        raise OSError("the folder went away mid-turn")

    monkeypatch.setattr(sns, "publish_next_steps", _boom)
    assert sns.refresh_from_turn(None, standing, goals=["a"],
                                 deep_results=[_deep(met=False)]) is None
    # The session keeps asserting the answer it read, since nothing replaced it.
    assert "Finish the packaging checklist" in standing.text


# --- the session wiring (shared by every UI) ---------------------------------
#
# InteractiveSession is the session brain every chat entry point constructs, so these drive its two
# methods directly on a stub rather than standing up an orchestrator: the point under test is that
# the artifact reaches rep_preamble and that a completed turn is offered back to it, not how a
# terminal draws.

class _StubConsole:
    def __init__(self):
        self.lines = []

    def dim(self, text):
        self.lines.append(text)


def _stub_session(standing, *, system=None, persona=None, client=None):
    return SimpleNamespace(
        _standing_next_steps=standing, _system=system, _persona=persona,
        _console=_StubConsole(), _quest_client=lambda: client,
    )


def test_the_artifact_reaches_every_turn_through_rep_preamble(tmp_path):
    standing = sns.load_standing_next_steps(_cfg(corpus_root=_folder(tmp_path)))
    session = _stub_session(standing, persona="You are Kai, who writes the release notes.")
    preamble = InteractiveSession._effective_preamble(session)
    assert "Finish the packaging checklist" in preamble
    assert "STANDING NEXT-STEPS ARTIFACT" in preamble
    # Persona first: the judge's lens truncates the preamble, and identity is what it must keep.
    assert preamble.index("You are Kai") < preamble.index("STANDING NEXT-STEPS ARTIFACT")


def test_a_session_with_no_artifact_composes_exactly_the_preamble_it_composed_before():
    session = _stub_session(None, persona="You are Kai.")
    assert InteractiveSession._effective_preamble(session) == "You are Kai."
    assert InteractiveSession._effective_preamble(_stub_session(None)) is None


def test_a_completed_deep_turn_refreshes_through_the_session(tmp_path):
    folder = _folder(tmp_path)
    standing = sns.load_standing_next_steps(_cfg(corpus_root=folder))
    client = _FakeQuestClient()
    session = _stub_session(standing, client=client)
    final = SimpleNamespace(kind="deep", goals=["cut the release"], deep_results=[_deep(met=False)])
    InteractiveSession._maybe_refresh_next_steps(session, final)
    assert "cut the release" in read_next_steps(folder)
    assert any("Standing next steps refreshed" in ln for ln in session._console.lines)
    # The next turn's preamble asserts the answer this turn wrote, not the one it replaced.
    assert "cut the release" in InteractiveSession._effective_preamble(session)


def test_small_talk_leaves_the_artifact_and_the_transcript_alone(tmp_path):
    folder = _folder(tmp_path)
    standing = sns.load_standing_next_steps(_cfg(corpus_root=folder))
    client = _FakeQuestClient()
    session = _stub_session(standing, client=client)
    final = SimpleNamespace(kind="answer", goals=[], deep_results=[])
    InteractiveSession._maybe_refresh_next_steps(session, final)
    assert "Finish the packaging checklist" in read_next_steps(folder)
    assert client.calls == []
    assert session._console.lines == []


def test_a_quest_side_failure_is_reported_and_does_not_raise(tmp_path):
    folder = _folder(tmp_path)
    standing = sns.load_standing_next_steps(_cfg(corpus_root=folder))
    session = _stub_session(standing, client=_FakeQuestClient(fail_on={"list"}))
    final = SimpleNamespace(kind="deep", goals=["cut the release"], deep_results=[_deep(met=False)])
    InteractiveSession._maybe_refresh_next_steps(session, final)
    assert "cut the release" in read_next_steps(folder)      # local copy is still correct
    assert any("On Quest:" in ln for ln in session._console.lines)


def test_a_session_with_no_artifact_refreshes_nothing():
    session = _stub_session(None, client=_FakeQuestClient())
    InteractiveSession._maybe_refresh_next_steps(
        session, SimpleNamespace(kind="deep", goals=["a"], deep_results=[_deep(met=False)]))
    assert session._console.lines == []


def test_the_turn_completion_path_actually_calls_it():
    """The first half of this feature shipped with its attended trigger left unwired, which is the
    whole reason it needed a second pass. Read as source rather than imported so the check holds
    without a terminal or the optional UI dependencies."""
    ui = (Path(quest_ai_runner.__file__).parent / "textual_ui.py").read_text(encoding="utf-8")
    assert "_maybe_refresh_next_steps(final)" in ui
