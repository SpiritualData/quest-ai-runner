"""A world seeded on an earlier day is re-seeded before a run, so relative dates in the cases hold."""
import datetime
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


def test_contents_seeded_before_today_are_stale():
    import world
    yesterday = (world.TODAY - datetime.timedelta(days=1)).isoformat()
    assert world.contents_are_stale({"contents_seeded_on": yesterday})
    assert not world.contents_are_stale({"contents_seeded_on": world.TODAY.isoformat()})
    assert not world.contents_are_stale({})
