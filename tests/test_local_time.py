"""``runner/local_time.py`` -- the only place ``zoneinfo`` is touched in this repo.

Pure, dependency-free, offline: pins the degrade-to-local-clock contract (never UTC, never a
raised exception) and the wall-clock construction ``scheduled_moment``/``now_in_zone`` do.
"""
from datetime import datetime, timezone

from quest_ai_runner.runner.local_time import (
    now_in_zone,
    resolve_zone,
    scheduled_moment,
    today_in_zone,
)


def test_resolve_zone_returns_a_working_zoneinfo_for_a_real_name():
    zone = resolve_zone("America/Los_Angeles")
    assert zone is not None
    assert str(zone) == "America/Los_Angeles"


def test_resolve_zone_returns_none_for_empty_or_absent():
    assert resolve_zone(None) is None
    assert resolve_zone("") is None
    assert resolve_zone("   ") is None


def test_resolve_zone_returns_none_and_warns_once_for_an_unknown_name(caplog):
    with caplog.at_level("WARNING"):
        assert resolve_zone("Not/AZone") is None
        assert resolve_zone("Not/AZone") is None   # second call: no additional warning
    assert caplog.text.count("could not be resolved") == 1


def test_now_in_zone_converts_an_aware_instant_correctly():
    aware = datetime(2026, 8, 14, 14, 0, tzinfo=timezone.utc)
    converted = now_in_zone("America/Los_Angeles", aware)
    assert converted.tzinfo is not None
    assert (converted.hour, converted.minute) == (7, 0)   # 14:00 UTC == 07:00 PDT


def test_now_in_zone_treats_a_naive_instant_as_utc():
    naive = datetime(2026, 8, 14, 14, 0)
    converted = now_in_zone("America/Los_Angeles", naive)
    assert converted.tzinfo is not None
    assert (converted.hour, converted.minute) == (7, 0)


def test_now_in_zone_falls_back_to_the_given_now_when_the_zone_is_unknown():
    naive = datetime(2026, 8, 14, 14, 0)
    assert now_in_zone("Not/AZone", naive) == naive


def test_now_in_zone_falls_back_to_the_wall_clock_when_nothing_is_given_and_the_zone_is_unknown():
    result = now_in_zone("Not/AZone", None)
    assert result.tzinfo is None   # the naive local datetime.now() fallback


def test_today_in_zone_is_the_date_of_now_in_zone():
    aware = datetime(2026, 8, 14, 14, 0, tzinfo=timezone.utc)  # 07:00 PDT -- still 2026-08-14
    assert today_in_zone("America/Los_Angeles", aware) == datetime(2026, 8, 14).date()

    # Late UTC evening is already the NEXT calendar day west of UTC -- unaffected here, but the
    # boundary case that matters: shortly after UTC midnight is still the PREVIOUS day in LA.
    just_after_utc_midnight = datetime(2026, 8, 14, 3, 0, tzinfo=timezone.utc)  # 20:00 PDT (13th)
    assert today_in_zone("America/Los_Angeles", just_after_utc_midnight) == datetime(2026, 8, 13).date()


def test_scheduled_moment_reads_a_wall_clock_time_in_the_named_zone():
    moment = scheduled_moment("2026-08-14", "06:30", "America/Los_Angeles")
    assert moment.tzinfo is not None
    assert (moment.year, moment.month, moment.day, moment.hour, moment.minute) == \
        (2026, 8, 14, 6, 30)


def test_scheduled_moment_falls_back_to_naive_when_the_zone_is_unknown():
    moment = scheduled_moment("2026-08-14", "06:30", "Not/AZone")
    assert moment == datetime(2026, 8, 14, 6, 30)
    assert moment.tzinfo is None


def test_scheduled_moment_missing_time_means_midnight():
    moment = scheduled_moment("2026-08-14", None, "America/Los_Angeles")
    assert (moment.hour, moment.minute) == (0, 0)


def test_scheduled_moment_none_for_an_unparseable_date():
    assert scheduled_moment("not-a-date", "06:30", "America/Los_Angeles") is None
    assert scheduled_moment("", "06:30", "America/Los_Angeles") is None


def test_scheduled_moment_dst_boundary_never_raises():
    """A local time that does not exist on a spring-forward day must resolve (never raise),
    through ZoneInfo's normal fold rules -- the documented, acceptable direction to err (late,
    never early)."""
    # 2026-03-08 02:30 America/Los_Angeles falls inside that year's spring-forward gap.
    moment = scheduled_moment("2026-03-08", "02:30", "America/Los_Angeles")
    assert moment is not None
    assert moment.tzinfo is not None
