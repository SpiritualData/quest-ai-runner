"""select_related's citation rendering: human-readable title + date over a bare internal id.

Found live (2026-09-11): a Quest AI reply cited a past conversation to the person as
"qaconv_f29626f14636" -- meaningless and unprofessional to read, even though the underlying
conversation document already carries a real ``title``. The fix threads that title (and a
formatted date, when the timestamp is parseable) into the rendered block AND the ``sources``
label, while keeping the raw id available (as a bracketed "ref") so the model can still act on it
this turn (e.g. re-reading the same conversation) without it leaking into a reply.
"""
from datetime import datetime, timezone

from quest_ai_runner.adapters.conversation_format import (
    conversation_timestamp,
    conversation_title,
    format_conversation_date,
    select_related,
)


def _conv(id_, title=None, updated_at=None, messages=None):
    conv = {"id": id_, "messages": messages or [{"role": "user", "content": "hello"}]}
    if title is not None:
        conv["title"] = title
    if updated_at is not None:
        conv["updated_at"] = updated_at
    return conv


def test_select_related_renders_title_and_date_when_available():
    conv = _conv("qaconv_f29626f14636", title="Accountability calls during exercise",
                 updated_at="2026-08-27T10:00:00Z")
    text, sources, _truncated = select_related([conv], "accountability calls")

    assert '"Accountability calls during exercise"' in text
    assert "2026-08-27" in text
    # The raw id is still present (as a bracketed ref, for the model's own later use), just not
    # the FIRST/only thing named.
    assert "[ref: qaconv_f29626f14636]" in text
    assert sources == [{"conv_id": "qaconv_f29626f14636",
                        "label": "Accountability calls during exercise"}]


def test_select_related_falls_back_to_id_when_no_title():
    conv = _conv("qaconv_abc123")  # no title field at all
    text, sources, _truncated = select_related([conv], "anything")

    assert text.startswith("=== Related conversation: qaconv_abc123 ===")
    assert sources == [{"conv_id": "qaconv_abc123", "label": "related conversation"}]


def test_select_related_omits_date_when_no_timestamp_but_keeps_title():
    conv = _conv("qaconv_xyz", title="Marathon training plan")  # no updated_at
    text, sources, _truncated = select_related([conv], "marathon")

    assert '"Marathon training plan"' in text
    assert "[ref: qaconv_xyz]" in text
    assert sources[0]["label"] == "Marathon training plan"


def test_conversation_title_ignores_blank_titles():
    assert conversation_title({"title": "   "}) is None
    assert conversation_title({"title": ""}) is None
    assert conversation_title({}) is None
    assert conversation_title({"title": "Real title"}) == "Real title"


def test_conversation_timestamp_accepts_datetime_object_not_just_epoch_number():
    # Found alongside the citation bug: a Mongo-backed ConversationStore hands back a real
    # datetime object for updated_at/created_at, not an epoch float -- the old implementation
    # only recognized int/float and silently returned 0.0 for every such backend, which broke
    # BOTH recency ranking (rank_candidates_by_digest) and date-only display for real deployments.
    dt = datetime(2026, 8, 27, 10, 0, 0, tzinfo=timezone.utc)
    ts = conversation_timestamp({"updated_at": dt})
    assert ts == dt.timestamp()


def test_conversation_timestamp_accepts_iso_string():
    ts = conversation_timestamp({"created_at": "2026-08-27T10:00:00Z"})
    expected = datetime(2026, 8, 27, 10, 0, 0, tzinfo=timezone.utc).timestamp()
    assert ts == expected


def test_conversation_timestamp_unparseable_degrades_to_zero_not_a_crash():
    assert conversation_timestamp({"updated_at": "not a date"}) == 0.0
    assert conversation_timestamp({"updated_at": None}) == 0.0
    assert conversation_timestamp({}) == 0.0


def test_format_conversation_date_is_short_and_utc():
    conv = {"updated_at": datetime(2026, 8, 27, 23, 59, 0, tzinfo=timezone.utc)}
    assert format_conversation_date(conv) == "2026-08-27"
    assert format_conversation_date({}) is None
