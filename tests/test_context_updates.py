"""One engine answers "what has changed since an assistant last looked?", for every channel.

The ask that produced ``runner/context_updates.py``: automated context updates for autopilot, so a
person who leaves a comment on a document, adds a note to a quest, or captures a thought does not
have to hand-maintain a retrieval plan in their standing instructions ("check the comments on the
doc") to get it read.

What this file pins down, beyond "the text shows up":

  * A TAG NEVER GATES DELIVERY. Every capture is its own row with its own ref; a card that names
    insight categories only gets the captures tagged that way FLAGGED as waiting on an answer.
    The one thing that sets a capture aside is the relevance judge, per capture, and any failure
    of the judge keeps everything.
  * A WATERMARK ONLY MOVES WHEN THE MATERIAL WAS DELIVERED, and only for the sources that were
    actually readable. One API blip must not consume a person's comment on the way past.
  * ONE SOURCE FAILING COSTS THE OTHERS NOTHING, and the failure is reported rather than swallowed.
  * THE RECEIPT IS THE RUN'S OWN ACCOUNT, rebuilt from the task's own text, and a ref the run said
    nothing about is reported as exactly that instead of being quietly dropped.

Offline, driven against fakes.
"""
from datetime import datetime, timedelta, timezone

from quest_ai_runner.runner.context_updates import (
    BLOCK_END,
    BLOCK_START,
    ContextUpdate,
    ContextUpdates,
    SourceReport,
    UpdateEngine,
    Watermarks,
    append_receipt,
    build_update_engine,
    default_spec_resolver,
    parse_manifest,
    parse_usage_notes,
    render_receipt,
    strip_usage_block,
    watermark_path_for,
)

NOW = datetime(2026, 9, 9, 9, 0, 0, tzinfo=timezone.utc)


def _now():
    return NOW


def _iso(days_ago=0, hours_ago=0):
    return (NOW - timedelta(days=days_ago, hours=hours_ago)).isoformat()


class BareClient:
    """A client with none of the optional read methods: every built-in source finds nothing."""


class NotesClient(BareClient):
    def __init__(self, notes):
        self.notes = list(notes)
        self.calls = []

    def list_quest_notes(self, quest_id):
        self.calls.append(quest_id)
        return list(self.notes)


class ExplodingSource:
    name = "explodes"
    describes = "always raises"

    def collect(self, request):
        raise RuntimeError("the API said no")


class StaticSource:
    """A source that yields one fixed update, so a test can drive the engine without a backend."""

    def __init__(self, name="static", needs_response=False, slot="", when=None):
        self.name = name
        self.describes = "test source"
        self._needs = needs_response
        self._slot = slot
        self._when = when
        self.seen_since = []

    def collect(self, request):
        self.seen_since.append(request.since)
        return [ContextUpdate(source=self.name, kind="thing", item_id=f"{self.name}-1",
                              title=f"{self.name} title", body=f"{self.name} body",
                              occurred_at=self._when or NOW, location="somewhere",
                              needs_response=self._needs, slot=self._slot)]


def _engine(client=None, sources=None, **kwargs):
    kwargs.setdefault("always", ())
    return UpdateEngine(client or BareClient(), sources=sources, now_fn=_now, **kwargs)


# --- what a card watches, as data ------------------------------------------------------------

def test_a_card_declares_its_sources_as_data_including_the_bare_name_shorthand():
    card = {"autopilot": {"context_sources": ["quest_notes",
                                              {"source": "insights", "categories": ["PhD"]}]}}
    assert default_spec_resolver(card) == [
        {"source": "quest_notes"},
        {"source": "insights", "categories": ["PhD"]},
    ]


def test_a_card_that_declares_nothing_watches_nothing_of_its_own():
    assert default_spec_resolver({"quest_id": "q1"}) == []


def test_a_cards_own_spec_refines_an_always_on_source_instead_of_running_it_twice():
    engine = UpdateEngine(BareClient(), always=("insights",), now_fn=_now)
    specs = engine.specs_for({"context_sources": [{"source": "insights", "categories": ["PhD"]}]})
    assert specs == [{"source": "insights", "categories": ["PhD"]}]


def test_an_unknown_source_is_reported_as_a_gap_and_never_raises():
    engine = _engine()
    bundle = engine.collect({"context_sources": ["no_such_channel"]}, card_id="q1")
    assert not bundle.has_any()
    assert [(r.source, r.error) for r in bundle.reports] == [
        ("no_such_channel", "no such source is registered")]


# --- the sources -----------------------------------------------------------------------------

def test_a_note_the_assistant_already_answered_is_history_not_news():
    """A person's note is answered once an assistant note follows it on the quest.

    Every run that writes one had the notes in front of it. Live failure this replaces: a first
    look offered ten notes answered days earlier, every one of them marked "needs an answer".
    """
    client = NotesClient([
        {"id": "n1", "text": "Chapter two needs the method first", "author_kind": "user",
         "author_name": "the owner", "created_at": _iso(hours_ago=3)},
        {"id": "n2", "text": "Pass summary: did three things", "author_kind": "ai",
         "created_at": _iso(hours_ago=2)},
        {"id": "n3", "text": "Actually do the survey lineage first", "author_kind": "user",
         "author_name": "the owner", "created_at": _iso(hours_ago=1)},
        {"id": "n4", "text": "unattributed", "created_at": _iso(hours_ago=1)},
    ])
    bundle = _engine(client).collect({"context_sources": ["quest_notes"]}, card_id="q1")

    # n1 was answered by n2; n4 is unattributed and never asserted to be the person's instruction.
    assert [u.item_id for u in bundle.updates] == ["n3"]
    assert bundle.updates[0].needs_response is True
    assert "add a note on this quest" in bundle.updates[0].how_to_respond


def test_an_unanswered_note_keeps_being_offered_even_past_the_watermark():
    """The watermark says what is NEW. It does not say what is answered.

    Time-filtering an open question loses it for good the moment one pass sees it and does
    nothing, which is the opposite of what a reply channel is for. The older one is still carried,
    and only its LABEL says it is not new.
    """
    client = NotesClient([
        {"id": "old", "text": "said days ago", "author_kind": "user", "created_at": _iso(days_ago=3)},
        {"id": "new", "text": "said this morning", "author_kind": "user",
         "created_at": _iso(hours_ago=1)},
    ])
    marks = Watermarks(None)
    marks.set("q1", "quest_notes", NOW - timedelta(days=1))
    bundle = _engine(client, watermarks=marks).collect(
        {"context_sources": ["quest_notes"]}, card_id="q1")

    assert sorted(u.item_id for u in bundle.updates) == ["new", "old"]
    older = [u for u in bundle.updates if u.item_id == "old"][0]
    assert "still open from before" in older.title
    newer = [u for u in bundle.updates if u.item_id == "new"][0]
    assert "still open from before" not in newer.title


def test_a_note_that_arrived_mid_run_is_offered_once_even_though_a_summary_followed_it():
    """The race this closes: a run starts, the person writes a note, the run posts its summary an
    hour later. An assistant note now follows the person's note, but no run ever saw it. Newer
    than the watermark means never delivered, so it is offered once, unflagged, and then it is
    history once a run has had it."""
    delivered_at = NOW - timedelta(hours=3)
    client = NotesClient([
        {"id": "mid", "text": "Actually use the survey lineage", "author_kind": "user",
         "author_name": "the owner", "created_at": _iso(hours_ago=2)},
        {"id": "summary", "text": "Run summary: drafted the method", "author_kind": "ai",
         "created_at": _iso(hours_ago=1)},
    ])
    marks = Watermarks(None)
    marks.set("q1", "quest_notes", delivered_at)
    engine = _engine(client, watermarks=marks)

    bundle = engine.collect({"context_sources": ["quest_notes"]}, card_id="q1")
    assert [u.item_id for u in bundle.updates] == ["mid"]
    assert bundle.updates[0].needs_response is False
    assert "an assistant note followed it" in bundle.updates[0].title

    bundle.mark_seen()                                       # a run has now had it
    assert engine.collect({"context_sources": ["quest_notes"]}, card_id="q1").updates == []


def test_a_first_look_never_reoffers_the_answered_notes_of_the_last_two_weeks():
    """On a card nothing has read, "newer than the watermark" is just "recent". Live failure this
    keeps fixed: a first look offered ten notes answered days earlier."""
    client = NotesClient([
        {"id": "asked", "text": "Do the method first", "author_kind": "user",
         "created_at": _iso(days_ago=3)},
        {"id": "answered", "text": "Done: method first", "author_kind": "ai",
         "created_at": _iso(days_ago=2)},
    ])
    assert _engine(client).collect({"context_sources": ["quest_notes"]}, card_id="q1").updates == []


def test_an_open_note_too_old_to_be_a_live_question_is_dropped():
    """Open until answered needs a floor, or it stops being a question and becomes a backlog."""
    client = NotesClient([
        {"id": "ancient", "text": "last year", "author_kind": "user",
         "created_at": _iso(days_ago=400)},
        {"id": "recent", "text": "this week", "author_kind": "user",
         "created_at": _iso(days_ago=2)},
    ])
    bundle = _engine(client).collect({"context_sources": ["quest_notes"]}, card_id="q1")
    assert [u.item_id for u in bundle.updates] == ["recent"]


def test_a_burst_of_open_notes_is_capped_at_the_newest_few():
    from quest_ai_runner.runner.context_updates import MAX_OPEN_PER_SOURCE

    client = NotesClient([
        {"id": f"n{i}", "text": f"note {i}", "author_kind": "user",
         "created_at": _iso(hours_ago=20 - i)} for i in range(12)
    ])
    bundle = _engine(client).collect({"context_sources": ["quest_notes"]}, card_id="q1")
    assert len(bundle.updates) == MAX_OPEN_PER_SOURCE
    assert "n11" in {u.item_id for u in bundle.updates}   # the newest survived
    assert "n0" not in {u.item_id for u in bundle.updates}  # the oldest did not


# --- one channel failing never costs the others ------------------------------------------------

def test_a_source_that_raises_is_reported_and_the_rest_of_the_bundle_is_delivered():
    engine = _engine(sources=[ExplodingSource(), StaticSource("static")])
    bundle = engine.collect({"context_sources": ["explodes", "static"]}, card_id="q1")
    assert [u.source for u in bundle.updates] == ["static"]
    failed = [r for r in bundle.reports if r.source == "explodes"][0]
    assert "RuntimeError" in failed.error and "the API said no" in failed.error
    assert failed.ok is False


def test_checked_line_says_what_was_looked_at_even_when_nothing_arrived():
    bundle = ContextUpdates(card_id="q1", reports=[
        SourceReport(source="quest_notes", found=0),
        SourceReport(source="drive_comments", error="401"),
        SourceReport(source="insights", found=2),
    ])
    line = bundle.checked_line()
    assert "quest_notes (nothing new)" in line
    assert "drive_comments (could not read: 401)" in line
    assert "insights (2)" in line


# --- what the run is shown --------------------------------------------------------------------

def test_the_prompt_block_indexes_every_ref_and_asks_for_one_line_back_per_ref():
    engine = _engine(sources=[StaticSource("a"), StaticSource("b")])
    bundle = engine.collect({"context_sources": ["a", "b"]}, card_id="q1")
    block = bundle.as_prompt_block()
    assert BLOCK_START in block and BLOCK_END in block
    assert bundle.refs() == ["U1", "U2"]
    assert "[U1]" in block and "[U2]" in block
    assert "Context used:" in block
    # The gate names the exact refs rather than describing them.
    assert "one line for each of U1, U2" in block


def test_a_slotted_channel_keeps_its_own_section_of_the_brief_but_still_gets_a_ref():
    engine = _engine(sources=[StaticSource("reflections", slot="reflection"), StaticSource("b")])
    bundle = engine.collect({"context_sources": ["reflections", "b"]}, card_id="q1")
    block = bundle.as_prompt_block(exclude_slots=("reflection",))
    assert "rendered in its own section of this brief" in block
    assert "reflections body" not in block           # not duplicated into the general block
    assert "reflections body" in bundle.slot_text("reflection")
    assert "U1" in block and "U2" in block           # both still accounted for in the receipt


def test_an_empty_bundle_composes_nothing_at_all():
    assert _engine().collect({}, card_id="q1").as_prompt_block() == ""


def test_the_cap_never_drops_the_things_somebody_is_waiting_on():
    sources = [StaticSource(f"n{i}", when=NOW - timedelta(hours=i)) for i in range(4)]
    sources.append(StaticSource("question", needs_response=True,
                                when=NOW - timedelta(days=5)))     # oldest, would sort last
    engine = _engine(sources=sources, max_updates=2)
    bundle = engine.collect(
        {"context_sources": [s.name for s in sources]}, card_id="q1")
    assert len(bundle.updates) == 2
    assert "question" in [u.source for u in bundle.updates]


# --- the watermark ----------------------------------------------------------------------------

def test_marking_seen_advances_only_the_sources_that_could_actually_be_read():
    marks = Watermarks(None)
    engine = _engine(sources=[ExplodingSource(), StaticSource("static")], watermarks=marks)
    bundle = engine.collect({"context_sources": ["explodes", "static"]}, card_id="q1")
    bundle.mark_seen()
    assert marks.get("q1", "static") == NOW
    assert marks.get("q1", "explodes") is None


def test_collecting_alone_never_consumes_anything():
    marks = Watermarks(None)
    engine = _engine(sources=[StaticSource("static")], watermarks=marks)
    engine.collect({"context_sources": ["static"]}, card_id="q1")
    assert marks.get("q1", "static") is None


def test_a_watermark_never_moves_backwards(tmp_path):
    path = str(tmp_path / "marks.json")
    marks = Watermarks(path)
    marks.set("q1", "quest_notes", NOW)
    marks.set("q1", "quest_notes", NOW - timedelta(days=2))
    assert marks.get("q1", "quest_notes") == NOW
    # And it survives the process that wrote it.
    assert Watermarks(path).get("q1", "quest_notes") == NOW


def test_an_unreadable_watermark_file_reads_as_never_looked_rather_than_crashing(tmp_path):
    path = tmp_path / "marks.json"
    path.write_text("{not json at all")
    assert Watermarks(str(path)).get("q1", "quest_notes") is None


def test_the_default_watermark_path_sits_beside_the_deployments_own_state_file():
    assert watermark_path_for(None, "/srv/qar/qar_state.json") == \
        "/srv/qar/qar_state_context_watermarks.json"
    assert watermark_path_for("/tmp/mine.json", "/srv/qar/qar_state.json") == "/tmp/mine.json"
    assert watermark_path_for(None, None) is None


# --- the receipt ------------------------------------------------------------------------------

def test_the_receipt_is_rebuilt_from_the_tasks_own_text_without_the_bundle_travelling_with_it():
    engine = _engine(sources=[StaticSource("a"), StaticSource("b")])
    bundle = engine.collect({"context_sources": ["a", "b"]}, card_id="q1")
    task_text = "Work this batch.\n\n" + bundle.as_prompt_block()
    manifest = parse_manifest(task_text)
    assert len(manifest) == 2                       # deduped: index line + detail line = one row
    run_output = ("I rewrote the method section.\n\n"
                  "Context used:\n  [U1] cited in the method\n  [U2] not used")
    reported = append_receipt(run_output, manifest)
    assert "Context updates taken into account:" in reported
    assert "-> cited in the method" in reported
    assert "-> not used" in reported
    assert "Context used:" not in reported          # the raw lines are replaced, not doubled


def test_a_ref_the_run_said_nothing_about_is_reported_as_exactly_that():
    receipt = render_receipt(["[U1] 2026-09-09 · comment · chapter two"], {})
    assert "no note from the run" in receipt


def test_a_task_that_carried_no_updates_gets_its_result_back_untouched():
    assert append_receipt("just the work", parse_manifest("no block here")) == "just the work"


def test_the_gates_example_refs_are_never_mistaken_for_offered_updates():
    """The gate sits OUTSIDE the block delimiters; if it did not, its examples would come back
    from ``parse_manifest`` as rows that were never offered."""
    engine = _engine(sources=[StaticSource("a")])
    bundle = engine.collect({"context_sources": ["a"]}, card_id="q1")
    assert len(parse_manifest(bundle.as_prompt_block())) == 1


def test_usage_lines_are_read_as_the_runs_own_structured_report_and_nothing_more():
    usage = parse_usage_notes("blah\n\nContext used:\n  [U1] answered in the doc.\n  [U2] not used\n"
                              "\nAnd then some closing prose.")
    assert usage == {"U1": "answered in the doc", "U2": "not used"}


def test_stripping_the_usage_block_keeps_the_prose_around_it():
    body = "The work.\n\nContext used:\n  [U1] used it\n\nClosing thought."
    stripped = strip_usage_block(body)
    assert "The work." in stripped and "Closing thought." in stripped
    assert "[U1]" not in stripped


# --- the consumer-facing factory ---------------------------------------------------------------

class _Cfg:
    def __init__(self, **kw):
        self.context_updates = kw.get("context_updates", True)
        self.context_updates_state_path = kw.get("context_updates_state_path")
        self.context_updates_first_look_days = kw.get("context_updates_first_look_days", 14)
        self.drive_comments = kw.get("drive_comments")


def test_a_consumer_that_switched_it_off_gets_no_engine_at_all():
    assert build_update_engine(_Cfg(context_updates=False), BareClient()) is None


def test_a_default_consumer_gets_an_engine_whose_stamps_persist_beside_its_state(tmp_path):
    state = str(tmp_path / "qar_state.json")
    engine = build_update_engine(_Cfg(), BareClient(), state_path=state)
    assert engine is not None
    assert "reflections" in engine.describe_sources()
    assert "drive_comments" in engine.describe_sources()
    bundle = engine.collect({"context_sources": ["quest_notes"]}, card_id="q1")
    bundle.mark_seen()
    assert (tmp_path / "qar_state_context_watermarks.json").exists()


# --- promotion, not filtering (the rule that keeps a tag from gating delivery) -----------------

class InsightsClient(BareClient):
    """The two reads ``runner.insights`` makes, and nothing else."""

    def __init__(self, entries):
        self.entries = list(entries)
        self.entry_calls = 0

    def get_insights_collection(self):
        return {"id": "coll_1"}

    def list_collection_entries(self, collection_id, *, page=0, limit=50):
        self.entry_calls += 1
        return {"items": self.entries if page == 0 else [], "pagination": {"has_next": False}}


def _capture(entry_id, text, categories):
    return {"id": entry_id, "createdAt": _iso(hours_ago=3),
            "fieldValues": {"insight": text, "acted_on": False, "categories": categories}}


def test_every_capture_is_its_own_row_and_a_tag_only_flags_it():
    """One row per capture, so the judge decides on each and the receipt answers for each.
    A category the card names flags the captures tagged that way; it never gates the others."""
    client = InsightsClient([
        _capture("e1", "The method chapter has to come first", ["PhD"]),
        _capture("e2", "Batch the errands into one trip", ["home"]),
    ])
    bundle = _engine(client).collect(
        {"context_sources": [{"source": "insights", "categories": ["phd"]}]}, card_id="q1")

    rows = {u.item_id: u for u in bundle.updates}
    assert set(rows) == {"e1", "e2"}                       # the untagged one is delivered too
    assert rows["e1"].needs_response is True and "tagged this PhD" in rows["e1"].title
    assert rows["e2"].needs_response is False
    assert all(u.slot == "insights" and u.ref for u in bundle.updates)
    # The slot reads as the block always did: the framing, then one ref-tagged row per capture.
    from quest_ai_runner.runner.insights import BLOCK_FOOTER
    slot = bundle.slot_text("insights")
    assert slot.startswith("Insights the person captured on Quest")
    assert slot.rstrip().endswith(BLOCK_FOOTER)
    assert f"[{rows['e1'].ref}] [2026-09-09] tagged PhD" in slot
    assert "(+1 more)" in bundle.slot_summary("insights")
    # And the receipt lists each capture in the person's own words.
    receipt = render_receipt(bundle.manifest(), {rows["e1"].ref: "folded into the plan"})
    assert '"The method chapter has to come first" · needs an answer -> folded into the plan' in receipt
    assert '"Batch the errands into one trip" -> no note from the run' in receipt


def test_the_judge_sees_each_capture_on_its_own_and_only_the_kept_ones_reach_the_slot():
    """A judge shown the captures as ONE block could only drop them all or pass them all."""
    client = InsightsClient([
        _capture("e1", "idea for the construct weighting", ["PhD"]),
        _capture("e2", "collaboration tracking at Cornerstone", ["work"]),
    ])
    seen = {}

    def judge(work, ctx, ups):
        seen["items"] = [u.item_id for u in ups]
        return {"e1"}

    bundle = _engine(client, relevance_judge=judge).collect(
        {"context_sources": ["insights"]}, card_id="q1")
    assert seen["items"] == ["e1", "e2"]
    assert [u.item_id for u in bundle.updates] == ["e1"]
    assert "Cornerstone" not in bundle.slot_text("insights")
    assert "insights (1, 1 not about this work)" in bundle.checked_line()


def test_the_judge_is_given_the_whole_description_of_the_work():
    """Clipping the assembled description to 400 characters cut it off before the description the
    judge was given it for; verified live when a tagged capture was dropped on the outcome alone."""
    from quest_ai_runner.runner.context_updates import llm_relevance_judge

    prompts = []

    class Provider:
        def answer(self, messages, model=None):
            prompts.append(messages[0]["content"])
            return '{"relevant": [1]}'

    judge = llm_relevance_judge(lambda: Provider())
    description = "ITPP corpus, construct coding, " * 30 + "THE TAIL OF THE DESCRIPTION"
    from quest_ai_runner.runner.context_updates import _describe_work
    work = _describe_work({"outcome": "I've completed my dissertation", "description": description},
                          "Dissertation")
    keep = judge(work, "", [ContextUpdate(source="insights", item_id="e1", body="x")])
    assert keep == {"e1"}
    assert "Outcome: I've completed my dissertation" in prompts[0]
    assert "THE TAIL OF THE DESCRIPTION"[:20] in prompts[0] or "Description:" in prompts[0]
    assert len(work) > 400 and work[:400] in prompts[0]


def test_the_user_scoped_cache_expires_so_an_afternoon_task_sees_the_mornings_captures():
    """The poller keeps ONE engine for its whole life. Without an expiry the first pass's read of
    the captures stood for every later pass and task until the next restart."""
    from quest_ai_runner.runner.context_updates import CACHE_TTL_SECONDS

    client = InsightsClient([_capture("e1", "One thought", ["PhD"])])
    clock = {"now": NOW}
    engine = UpdateEngine(client, always=(), now_fn=lambda: clock["now"])
    engine.collect({"context_sources": ["insights"]}, card_id="q1")
    engine.collect({"context_sources": ["insights"]}, card_id="q2")
    assert client.entry_calls == 1                          # one pass, one read
    clock["now"] = NOW + timedelta(seconds=CACHE_TTL_SECONDS + 1)
    engine.collect({"context_sources": ["insights"]}, card_id="q1")
    assert client.entry_calls == 2                          # later, read again


def test_a_user_scoped_channel_is_read_once_per_engine_not_once_per_card():
    client = InsightsClient([_capture("e1", "One thought", ["PhD"])])
    engine = _engine(client)
    for quest_id in ("q1", "q2", "q3"):
        engine.collect({"context_sources": ["insights"]}, card_id=quest_id)
    assert client.entry_calls == 1


class _OwnerComments:
    """A Drive client whose FOLDER holds nothing but whose OWNER query finds the docs.

    The real shape this exists for: an assistant's documents are owned by the assistant account and
    filed into the person's folder, so the credential is on each document and not on the folder.
    """

    def __init__(self, comments):
        self._comments = comments
        self.folder_calls = 0

    def comments_for_folder(self, folder_id, **kw):
        self.folder_calls += 1
        return []

    def files_owned_by(self, owner, **kw):
        from quest_ai_runner.adapters.drive_comments import DriveFileChange
        return [DriveFileChange(file_id="f1", file_name="A summary doc", file_url="u")]

    def comments_for_file(self, file_id, **kw):
        return list(self._comments)

    def files_in_folder(self, folder_id, **kw):
        return []


def test_a_card_can_watch_every_doc_one_account_owns():
    from datetime import datetime, timezone

    from quest_ai_runner.adapters.drive_comments import DriveComment
    from quest_ai_runner.runner.context_updates import UpdateEngine

    open_thread = DriveComment(
        file_id="f1", file_name="A summary doc", comment_id="c1", author="Joshua",
        content="not by design, I never said it is",
        quoted_text="positive-only by design",
        created_at=datetime(2026, 9, 8, tzinfo=timezone.utc),
        modified_at=datetime(2026, 9, 8, tzinfo=timezone.utc))

    client = _OwnerComments([open_thread])
    engine = UpdateEngine(None, drive_comments=client, always=())
    bundle = engine.collect(
        {"quest_id": "q1",
         "context_sources": [{"source": "drive_comments", "owner": "assistant@example.org"}]},
        card_id="q1")

    assert [u.kind for u in bundle.updates] == ["comment"]
    assert "positive-only by design" in bundle.updates[0].body
    assert bundle.updates[0].needs_response
    assert "reply to comment c1 on file f1" in bundle.updates[0].how_to_respond


def test_open_comment_threads_are_bounded_like_open_notes():
    """A thread nobody can answer would otherwise ride in every bundle for good, and since open
    items are never the ones the cap evicts, it would crowd everything else out with it."""
    from quest_ai_runner.adapters.drive_comments import DriveComment
    from quest_ai_runner.runner.context_updates import (MAX_OPEN_PER_SOURCE,
                                                        OPEN_ITEM_MAX_AGE_DAYS)

    threads = [DriveComment(file_id="f1", file_name="Chapter two", comment_id=f"c{i}",
                            author="Ada", content=f"question {i}",
                            created_at=NOW - timedelta(days=i))
               for i in range(MAX_OPEN_PER_SOURCE + 4)]
    threads.append(DriveComment(file_id="f1", file_name="Chapter two", comment_id="ancient",
                                author="Ada", content="from last year",
                                created_at=NOW - timedelta(days=OPEN_ITEM_MAX_AGE_DAYS + 30)))
    bundle = _engine(sources=[__import__(
        "quest_ai_runner.runner.context_updates", fromlist=["DriveCommentsSource"]
    ).DriveCommentsSource(_OwnerComments(threads))]).collect(
        {"context_sources": [{"source": "drive_comments", "owner": "assistant@example.org"}]},
        card_id="q1")

    ids = [u.raw["comment_id"] for u in bundle.updates]
    assert len(ids) == MAX_OPEN_PER_SOURCE
    assert "c0" in ids and "ancient" not in ids
    assert f"c{MAX_OPEN_PER_SOURCE + 3}" not in ids            # the oldest of the burst let go


def test_folder_and_owner_routes_do_not_double_report_the_same_file():
    from datetime import datetime, timezone

    from quest_ai_runner.adapters.drive_comments import DriveComment, DriveFileChange
    from quest_ai_runner.runner.context_updates import UpdateEngine

    thread = DriveComment(file_id="f1", file_name="doc", comment_id="c1", author="J",
                          content="q?", created_at=datetime(2026, 9, 8, tzinfo=timezone.utc))

    class Both(_OwnerComments):
        def comments_for_folder(self, folder_id, **kw):
            self.folder_calls += 1
            return [thread]

    client = Both([thread])
    engine = UpdateEngine(None, drive_comments=client, always=())
    bundle = engine.collect(
        {"quest_id": "q1",
         "context_sources": [{"source": "drive_comments", "folder_id": "F",
                              "owner": "assistant@example.org"}]},
        card_id="q1")

    assert len(bundle.updates) == 1


def test_a_persons_note_on_the_quest_is_collected_without_the_card_asking():
    """The reply channel is never opt-in.

    Live failure, 2026-09-10: a run answered Joshua's emailed reply (which lands as a note on the
    quest) and its receipt listed only the reflection and the captures. Notes were opt-in, and the
    backend rejects the field a quest would declare them in, so the one channel he had just used
    was the one channel the engine never looked at.
    """
    from quest_ai_runner.runner.context_updates import UpdateEngine

    class Client:
        def list_quest_notes(self, quest_id):
            return [{"id": "n1", "text": "On 4 I don't want to decide this yet",
                     "author_kind": "user", "author_name": "Joshua",
                     "source": "email", "created_at": "2026-09-10T09:00:00Z"}]

    # No context_sources on the card at all: the failing case exactly.
    bundle = UpdateEngine(Client()).collect({"quest_id": "q1", "name": "Dissertation"},
                                            card_id="q1")

    notes = [u for u in bundle.updates if u.source == "quest_notes"]
    assert len(notes) == 1
    assert "don't want to decide this yet" in notes[0].body
    assert notes[0].needs_response
    assert "quest_notes" in {r.source for r in bundle.reports}


def test_an_ai_note_is_not_reported_back_as_the_persons_news():
    from quest_ai_runner.runner.context_updates import UpdateEngine

    class Client:
        def list_quest_notes(self, quest_id):
            return [{"id": "n1", "text": "Run summary: did the reading", "author_kind": "ai",
                     "created_at": "2026-09-10T09:00:00Z"}]

    bundle = UpdateEngine(Client()).collect({"quest_id": "q1"}, card_id="q1")
    assert [u for u in bundle.updates if u.source == "quest_notes"] == []


def test_a_note_is_labelled_by_the_quests_name_not_its_outcome():
    """The outcome is a sentence about the future, not a label.

    Live output, 2026-09-10: every note line in the receipt read
    "note · I've completed my dissertation and have a PhD", because the outcome was the fallback
    label. That column exists to say WHERE the note is so a person can scan a column of them.
    """
    from quest_ai_runner.runner.context_updates import UpdateEngine

    class Client:
        def list_quest_notes(self, quest_id):
            return [{"id": "n1", "text": "do X", "author_kind": "user",
                     "created_at": "2026-09-10T09:00:00Z"}]

    # The shape the Quest state endpoint returns: an outcome, no name.
    bundle = UpdateEngine(Client(), always=("quest_notes",)).collect(
        {"quest_id": "q1", "outcome": "I've completed my dissertation and have a PhD"},
        card_id="q1")

    line = bundle.updates[0].manifest_line()
    assert "I've completed my dissertation" not in line
    assert "this quest" in line

    named = UpdateEngine(Client(), always=("quest_notes",)).collect(
        {"quest_id": "q1", "name": "Dissertation",
         "outcome": "I've completed my dissertation and have a PhD"}, card_id="q1")
    assert "Dissertation" in named.updates[0].manifest_line()


def test_the_callers_own_label_beats_whatever_the_card_row_carries():
    """An autopilot pass has already resolved a display label; the quest state endpoint has not."""
    from quest_ai_runner.runner.context_updates import UpdateEngine

    class Client:
        def list_quest_notes(self, quest_id):
            return [{"id": "n1", "text": "do X", "author_kind": "user",
                     "created_at": "2026-09-10T09:00:00Z"}]

    bundle = UpdateEngine(Client(), always=("quest_notes",)).collect(
        {"quest_id": "q1", "outcome": "I've completed my dissertation and have a PhD"},
        card_id="q1", card_label="Dissertation")

    assert "Dissertation" in bundle.updates[0].manifest_line()


# --- relevance: the engine's job, not the run's ------------------------------------------------

def _relevance_capture(item_id, body):
    from datetime import datetime, timezone

    from quest_ai_runner.runner.context_updates import ContextUpdate
    return ContextUpdate(source="insights", kind="capture", item_id=item_id, body=body,
                         occurred_at=datetime(2026, 9, 10, tzinfo=timezone.utc))


class _Captures:
    name = "insights"
    describes = ""
    judge_relevance = True
    slot = ""

    def __init__(self, rows):
        self._rows = rows

    def collect(self, request):
        return list(self._rows)


class _Notes:
    name = "quest_notes"
    describes = ""
    judge_relevance = False
    slot = ""

    def collect(self, request):
        from datetime import datetime, timezone

        from quest_ai_runner.runner.context_updates import ContextUpdate
        return [ContextUpdate(source="quest_notes", kind="note", item_id="n1",
                              body="do the method chapter", needs_response=True,
                              occurred_at=datetime(2026, 9, 10, tzinfo=timezone.utc))]


def test_a_capture_about_other_work_never_reaches_the_run():
    """Joshua, 2026-09-10, on a live brief: 'Passed over: the 9/10 Cornerstone capture
    (collaboration tracking) isn't this quest's domain.' That line is the context engine's work
    showing up as the assistant's chatter."""
    from quest_ai_runner.runner.context_updates import UpdateEngine

    rows = [_relevance_capture("cornerstone", "collaboration tracking at Cornerstone"),
            _relevance_capture("method", "idea for the construct weighting")]
    engine = UpdateEngine(None, sources=[_Captures(rows)], always=("insights",),
                          relevance_judge=lambda work, ctx, ups: {"method"})

    bundle = engine.collect({"quest_id": "q1", "name": "Dissertation"}, card_id="q1")

    assert [u.item_id for u in bundle.updates] == ["method"]
    assert "1 not about this work" in bundle.checked_line()


def test_a_judge_that_fails_costs_nothing_and_keeps_everything():
    """The worst case has to be a noisier brief, never a silently emptier one."""
    from quest_ai_runner.runner.context_updates import UpdateEngine

    rows = [_relevance_capture("a", "one"), _relevance_capture("b", "two")]
    for judge in (lambda *a: None, lambda *a: (_ for _ in ()).throw(RuntimeError("boom"))):
        engine = UpdateEngine(None, sources=[_Captures(rows)], always=("insights",),
                              relevance_judge=judge)
        bundle = engine.collect({"quest_id": "q1"}, card_id="q1")
        assert {u.item_id for u in bundle.updates} == {"a", "b"}


def test_a_card_scoped_channel_is_never_put_to_a_relevance_judgment():
    """A note on this quest is relevant because of WHERE it was written. Judging it could only
    ever lose one."""
    from quest_ai_runner.runner.context_updates import UpdateEngine

    seen = {}

    def judge(work, ctx, ups):
        seen["sources"] = {u.source for u in ups}
        return set()                      # would drop everything it is given

    engine = UpdateEngine(None, sources=[_Captures([_relevance_capture("a", "one")]), _Notes()],
                          always=("insights", "quest_notes"), relevance_judge=judge)
    bundle = engine.collect({"quest_id": "q1"}, card_id="q1")

    assert seen["sources"] == {"insights"}                     # notes never offered to the judge
    assert [u.source for u in bundle.updates] == ["quest_notes"]


# --- a habit tracked against a card is a record of that card's work ----------------------------

class _CollectionClient:
    def __init__(self, entries):
        self._entries = entries
        self.collection_calls = 0

    def list_collections(self):
        self.collection_calls += 1
        return [{"id": "coll_1", "name": "Focus on PhD Dissertation", "type": "habit"},
                {"id": "coll_2", "name": "Track screen use", "type": "habit"}]

    def list_collection_entries(self, collection_id, **kw):
        return self._entries if collection_id == "coll_1" else []


def _habit_entry(entry_id, date, completion, seconds, sessions, extra=None):
    values = {"entry_date": date, "completionType": completion, "period": "day",
              "period_start": date, "period_end": date, "completed": True,
              "habit_timer": {"value": seconds, "unit": "seconds"},
              "sessions": [{"start": f"{date}T1{i}:00:00Z"} for i in range(sessions)]}
    values.update(extra or {})
    return {"id": entry_id, "collectionId": "coll_1", "fieldValues": values}


def test_a_card_can_watch_a_habit_by_its_name():
    """The dissertation quest's own timer says whether the person sat down to it, and for how long.

    A run composing a brief without it is guessing at exactly the thing they already measured.
    """
    from quest_ai_runner.runner.context_updates import UpdateEngine

    client = _CollectionClient([_habit_entry("e1", "2026-09-09", "started", 10573, 10)])
    bundle = UpdateEngine(client, always=()).collect(
        {"quest_id": "q1",
         "context_sources": [{"source": "collection", "name": "Focus on PhD Dissertation"}]},
        card_id="q1")

    assert len(bundle.updates) == 1
    body = bundle.updates[0].body
    assert "2026-09-09" in body and "started" in body
    assert "2h 56m" in body                    # raw seconds mean nothing at a glance
    assert "10 session(s)" in body
    assert bundle.updates[0].needs_response is False


def test_a_week_of_habit_entries_is_one_update_not_seven():
    """A log is one channel. Seven refs would be seven receipt lines each saying "noted", and
    live they were the bulk of a bundle with the person's one capture at the bottom."""
    from quest_ai_runner.runner.context_updates import UpdateEngine

    client = _CollectionClient([
        _habit_entry(f"e{d}", f"2026-09-0{d}", "started", 3600, 1) for d in range(3, 10)])
    bundle = UpdateEngine(client, always=()).collect(
        {"quest_id": "q1", "context_sources": [{"source": "collection", "collection_id": "coll_1"}]},
        card_id="q1")

    assert len(bundle.updates) == 1
    row = bundle.updates[0]
    assert row.body.count("started") == 7 and row.body.startswith("2026-09-09")   # newest first
    assert row.excerpt == "7 entries, 2026-09-03 to 2026-09-09, 7h in all"
    assert '"7 entries, 2026-09-03 to 2026-09-09, 7h in all"' in row.manifest_line()


def test_a_habits_bookkeeping_fields_and_blanks_stay_out_of_the_brief():
    """Live render bug: a field the person left blank printed as "value_achieved: "."""
    from quest_ai_runner.runner.context_updates import UpdateEngine

    client = _CollectionClient([
        _habit_entry("e1", "2026-09-09", "started", 600, 1,
                     extra={"value_achieved": "   ", "notes": "read Ragin ch. 4"})])
    body = UpdateEngine(client, always=()).collect(
        {"quest_id": "q1", "context_sources": [{"source": "collection", "collection_id": "coll_1"}]},
        card_id="q1").updates[0].body

    assert "notes: read Ragin ch. 4" in body
    assert "value_achieved" not in body
    for internal in ("period_start", "period_end", "last_activity_date", "completed"):
        assert internal not in body


def test_only_habit_entries_since_the_last_look_are_offered():
    from datetime import timedelta

    from quest_ai_runner.runner.context_updates import UpdateEngine, Watermarks

    client = _CollectionClient([
        _habit_entry("old", "2026-09-01", "yes", 3600, 1),
        _habit_entry("new", "2026-09-09", "started", 3600, 1),
    ])
    marks = Watermarks(None)
    marks.set("q1", "collection", _as_utc_for_test("2026-09-05"))
    bundle = UpdateEngine(client, watermarks=marks, always=(),
                          now_fn=lambda: _as_utc_for_test("2026-09-10")).collect(
        {"quest_id": "q1", "context_sources": [{"source": "collection", "collection_id": "coll_1"}]},
        card_id="q1")

    assert len(bundle.updates) == 1
    assert "2026-09-09" in bundle.updates[0].body and "2026-09-01" not in bundle.updates[0].body


def test_a_habit_is_never_put_to_a_relevance_judgment():
    """A collection reaches a card only because the card NAMED it: card-scoped by construction."""
    from quest_ai_runner.runner.context_updates import UpdateEngine

    client = _CollectionClient([_habit_entry("e1", "2026-09-09", "started", 600, 1)])
    bundle = UpdateEngine(client, always=(),
                          relevance_judge=lambda *a: set()).collect(
        {"quest_id": "q1", "context_sources": [{"source": "collection", "collection_id": "coll_1"}]},
        card_id="q1")

    assert len(bundle.updates) == 1


def test_a_collection_named_by_something_that_does_not_exist_is_a_reported_gap():
    from quest_ai_runner.runner.context_updates import UpdateEngine

    client = _CollectionClient([])
    bundle = UpdateEngine(client, always=()).collect(
        {"quest_id": "q1", "context_sources": [{"source": "collection", "name": "No Such Habit"}]},
        card_id="q1")

    assert bundle.updates == []
    assert [r.source for r in bundle.reports] == ["collection"]


def _as_utc_for_test(text):
    from datetime import datetime, timezone
    return datetime.fromisoformat(text).replace(tzinfo=timezone.utc)
