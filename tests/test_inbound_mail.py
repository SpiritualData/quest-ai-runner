"""Mail arriving at a mailbox is a context source too, once real people are told apart from bulk.

Joshua's own framing: "Everything should make it into quests, even if from different sources...
even stuff from external people who send to support, but should be real people not marketing
emails etc, and an LLM will have to make that distinction." What this file pins down:

  * THE HEADER CHECK COSTS NO MODEL CALL. ``is_bulk_mail`` catches List-Unsubscribe,
    Precedence: bulk, and Auto-Submitted mail before a ``ContextUpdate`` is even built, so it is
    provably impossible for the admission judge to have been consulted about it.
  * ADMISSION AND RELEVANCE ARE DIFFERENT GATES. The admission judge decides "is anybody home",
    never "does this bear on this card" -- ``InboundMailSource.judge_relevance`` stays False.
  * A JUDGE FAILURE KEEPS EVERYTHING, the same rule ``llm_relevance_judge`` already lives by, for
    the identical reason: a dropped request from a real person is invisible.
  * RELAYING IS IDEMPOTENT, keyed by (card_id, source, item_id) the same way a watermark is keyed,
    because a relay is a WRITE with a real person's name on it -- a second pass over the same mail
    must never turn into a second note.
  * THE OWN REPLY MAILBOX IS REFUSED OUTRIGHT, so a card can never be pointed at the same address a
    consumer's own inbound-reply service already polls.

Offline throughout: no network, no real mailbox, no model provider.
"""
from datetime import datetime, timezone

from quest_ai_runner.adapters.inbound_mail import MailMessage, is_bulk_mail, refuses_as_inbound_mailbox
from quest_ai_runner.runner.context_updates import (
    InboundMailSource,
    RelayedItems,
    UpdateEngine,
    Watermarks,
)

NOW = datetime(2026, 9, 14, 12, 0, 0, tzinfo=timezone.utc)


def _now():
    return NOW


def _msg(message_id, sender_email, body_text, *, subject="", headers=None,
        sender_name="", received_at=None):
    return MailMessage(
        message_id=message_id, sender_email=sender_email, sender_name=sender_name,
        subject=subject, body_text=body_text, received_at=received_at or NOW,
        headers=headers or {},
    )


class FakeMailClient:
    """Stands in for ``adapters.inbound_mail.InboundMail``: no network, canned messages."""

    def __init__(self, messages):
        self.messages = list(messages)
        self.calls = []

    def messages_since(self, mailbox, *, since=None, max_messages=25):
        self.calls.append((mailbox, since, max_messages))
        return list(self.messages)


class FakeQuestClient:
    """Stands in for ``QuestClient``: records every relayed note, never touches the network."""

    def __init__(self):
        self.notes = []

    def add_quest_note(self, quest_id, text, *, author_label=None, relayed_author_email=None):
        self.notes.append(
            {"quest_id": quest_id, "text": text, "relayed_author_email": relayed_author_email})
        return []


def _card():
    return {"quest_id": "q1", "name": "Support",
           "context_sources": [{"source": "inbound_mail", "mailbox": "support@example.org"}]}


def _engine(mail_client, *, quest_client=None, admission_judge=None, relay_log=None,
           watermarks=None):
    return UpdateEngine(
        quest_client, sources=[InboundMailSource(mail_client)], always=(), now_fn=_now,
        admission_judge=admission_judge, relay_log=relay_log, watermarks=watermarks,
    )


# --- the deterministic header pre-filter, before any model call --------------------------------

def test_is_bulk_mail_reads_headers_only():
    assert is_bulk_mail({"list-unsubscribe": "<mailto:x@y.com>"})
    assert is_bulk_mail({"list-id": "<newsletter.example.org>"})
    assert is_bulk_mail({"precedence": "bulk"})
    assert is_bulk_mail({"auto-submitted": "auto-replied"})
    assert is_bulk_mail({"x-autoreply": "yes"})
    assert not is_bulk_mail({"auto-submitted": "no"})
    assert not is_bulk_mail({})
    assert not is_bulk_mail({"subject": "buy now, unsubscribe if you must"})  # never a word rule


def test_bulk_mail_is_dropped_before_a_context_update_even_exists():
    """No admission judge is wired at all here -- proof that filtering the newsletter out cannot
    have cost a model call, because there is no judge available to call."""
    messages = [
        _msg("m1", "person@example.org", "Can someone help me reset my password?"),
        _msg("m2", "newsletter@example.org", "50% off everything this week!",
             headers={"list-unsubscribe": "<mailto:off@example.org>"}),
    ]
    engine = _engine(FakeMailClient(messages))
    bundle = engine.collect(_card(), card_id="q1")

    assert [u.item_id for u in bundle.updates] == ["m1"]
    report = [r for r in bundle.reports if r.source == "inbound_mail"][0]
    assert report.considered == 2
    assert "filtered as bulk" in report.explanation


def test_a_mailbox_with_only_bulk_mail_reports_why_nothing_arrived():
    messages = [_msg("m1", "newsletter@example.org", "News!",
                     headers={"precedence": "bulk"})]
    engine = _engine(FakeMailClient(messages))
    bundle = engine.collect(_card(), card_id="q1")

    assert bundle.updates == []
    report = [r for r in bundle.reports if r.source == "inbound_mail"][0]
    assert "all 1 filtered as bulk" in report.explanation


# --- the admission judge: a different question from relevance ----------------------------------

def test_the_admission_judge_admits_a_real_person_and_drops_the_rest():
    messages = [
        _msg("m1", "person@example.org", "Could you look into my account, please?"),
        _msg("m2", "promo@example.org", "Check out our new spring collection"),
    ]
    engine = _engine(FakeMailClient(messages), admission_judge=lambda ups: {"m1"})
    bundle = engine.collect(_card(), card_id="q1")

    assert [u.item_id for u in bundle.updates] == ["m1"]


def test_a_judge_that_returns_none_keeps_everything():
    """None is the documented "could not judge" signal, not "admit nothing"."""
    messages = [_msg("m1", "a@example.org", "one"), _msg("m2", "b@example.org", "two")]
    engine = _engine(FakeMailClient(messages), admission_judge=lambda ups: None)
    bundle = engine.collect(_card(), card_id="q1")

    assert {u.item_id for u in bundle.updates} == {"m1", "m2"}


def test_a_judge_that_raises_keeps_everything():
    def boom(ups):
        raise RuntimeError("model unavailable")

    messages = [_msg("m1", "a@example.org", "one"), _msg("m2", "b@example.org", "two")]
    engine = _engine(FakeMailClient(messages), admission_judge=boom)
    bundle = engine.collect(_card(), card_id="q1")

    assert {u.item_id for u in bundle.updates} == {"m1", "m2"}


def test_no_admission_judge_configured_also_keeps_everything():
    messages = [_msg("m1", "a@example.org", "one")]
    engine = _engine(FakeMailClient(messages), admission_judge=None)
    bundle = engine.collect(_card(), card_id="q1")

    assert [u.item_id for u in bundle.updates] == ["m1"]


def test_inbound_mail_is_never_put_to_the_relevance_judge():
    """Card-scoped: it arrived at THIS card's own mailbox, so relevance would only ever lose one."""
    assert InboundMailSource().judge_relevance is False


# --- relaying an admitted item onto the quest, idempotently -------------------------------------

def test_an_admitted_item_is_relayed_onto_the_quest_under_the_senders_own_address():
    messages = [_msg("m1", "person@example.org", "Please cancel my subscription.",
                     sender_name="Pat")]
    quest_client = FakeQuestClient()
    engine = _engine(FakeMailClient(messages), quest_client=quest_client,
                     relay_log=RelayedItems(None))
    bundle = engine.collect(_card(), card_id="q1")
    bundle.mark_seen()

    assert len(quest_client.notes) == 1
    note = quest_client.notes[0]
    assert note["quest_id"] == "q1"
    assert note["relayed_author_email"] == "person@example.org"
    assert "cancel my subscription" in note["text"]


def test_relaying_the_same_item_twice_posts_only_one_note():
    messages = [_msg("m1", "person@example.org", "Please cancel my subscription.")]
    quest_client = FakeQuestClient()
    relay_log = RelayedItems(None)
    marks = Watermarks(None)

    engine = _engine(FakeMailClient(messages), quest_client=quest_client, relay_log=relay_log,
                     watermarks=marks)
    engine.collect(_card(), card_id="q1").mark_seen()
    # A second pass, the mail client still returning the SAME message (e.g. a "still owed"
    # re-offer, or a poll that has not advanced its own dedup) -- the relay must not repeat.
    engine.collect(_card(), card_id="q1").mark_seen()

    assert len(quest_client.notes) == 1


def test_relaying_persists_across_a_restart(tmp_path):
    path = str(tmp_path / "relayed.json")
    messages = [_msg("m1", "person@example.org", "Please cancel my subscription.")]

    quest_client_a = FakeQuestClient()
    engine_a = _engine(FakeMailClient(messages), quest_client=quest_client_a,
                       relay_log=RelayedItems(path))
    engine_a.collect(_card(), card_id="q1").mark_seen()
    assert len(quest_client_a.notes) == 1

    # A fresh process, a fresh RelayedItems instance over the SAME file: it must remember.
    quest_client_b = FakeQuestClient()
    engine_b = _engine(FakeMailClient(messages), quest_client=quest_client_b,
                       relay_log=RelayedItems(path))
    engine_b.collect(_card(), card_id="q1").mark_seen()
    assert quest_client_b.notes == []


def test_relaying_never_happens_at_collection_time_only_at_mark_seen():
    """Collecting alone must be a read -- exactly the property that makes
    ``quest-ai-runner context <quest_id>`` (a read-only engine that never calls mark_seen) safe."""
    messages = [_msg("m1", "person@example.org", "hello")]
    quest_client = FakeQuestClient()
    engine = _engine(FakeMailClient(messages), quest_client=quest_client,
                     relay_log=RelayedItems(None))
    engine.collect(_card(), card_id="q1")   # no mark_seen()

    assert quest_client.notes == []


def test_a_relay_failure_is_swallowed_and_the_item_is_retried_next_time():
    class BrokenQuestClient:
        def add_quest_note(self, quest_id, text, *, author_label=None,
                           relayed_author_email=None):
            raise RuntimeError("network down")

    messages = [_msg("m1", "person@example.org", "hello")]
    relay_log = RelayedItems(None)
    engine = _engine(FakeMailClient(messages), quest_client=BrokenQuestClient(),
                     relay_log=relay_log)
    bundle = engine.collect(_card(), card_id="q1")
    bundle.mark_seen()   # must not raise

    assert not relay_log.already_relayed("q1", "inbound_mail", "m1")


# --- the watermark still advances on delivery, same as every other source ----------------------

def test_the_watermark_advances_after_delivery():
    marks = Watermarks(None)
    engine = _engine(FakeMailClient([_msg("m1", "a@example.org", "hi")]), watermarks=marks)
    bundle = engine.collect(_card(), card_id="q1")
    bundle.mark_seen()

    assert marks.get("q1", "inbound_mail") == NOW


def test_collecting_alone_never_advances_the_watermark():
    marks = Watermarks(None)
    engine = _engine(FakeMailClient([_msg("m1", "a@example.org", "hi")]), watermarks=marks)
    engine.collect(_card(), card_id="q1")

    assert marks.get("q1", "inbound_mail") is None


# --- the reply mailbox is refused, not silently double-ingested ---------------------------------

def test_refuses_as_inbound_mailbox_catches_a_bare_reply_address():
    assert refuses_as_inbound_mailbox("ai@example.org") is not None


def test_refuses_as_inbound_mailbox_catches_a_tagged_reply_address():
    assert refuses_as_inbound_mailbox("ai+q-dissertation-7f3k9xq2m@example.org") is not None


def test_refuses_as_inbound_mailbox_allows_an_ordinary_mailbox():
    assert refuses_as_inbound_mailbox("support@example.org") is None


def test_a_card_pointed_at_the_reply_mailbox_is_a_reported_error_not_a_silent_ingest():
    card = {"quest_id": "q1",
           "context_sources": [{"source": "inbound_mail", "mailbox": "ai@example.org"}]}
    engine = _engine(FakeMailClient([_msg("m1", "a@example.org", "hi")]))
    bundle = engine.collect(card, card_id="q1")

    assert bundle.updates == []
    report = [r for r in bundle.reports if r.source == "inbound_mail"][0]
    assert report.error
    assert "reply address" in report.error


def test_the_reply_mailbox_refusal_is_checked_before_the_client_is_ever_consulted():
    client = FakeMailClient([_msg("m1", "a@example.org", "hi")])
    card = {"quest_id": "q1",
           "context_sources": [{"source": "inbound_mail", "mailbox": "ai@example.org"}]}
    engine = _engine(client)
    engine.collect(card, card_id="q1")

    assert client.calls == []


# --- discoverability -----------------------------------------------------------------------

def test_inbound_mail_is_advertised_by_the_default_engine():
    engine = UpdateEngine(None)
    assert "inbound_mail" in engine.describe_sources()
