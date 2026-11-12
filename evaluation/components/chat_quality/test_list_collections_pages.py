"""list_collections reads every page of the paged collections listing (teardown missed page 2)."""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ.setdefault("QUAL_WORK_DIR", "/tmp/qualeval_test_pages")

import devclient  # noqa: E402


def page_body(ids, has_next):
    return {"success": True, "data": {"items": [{"id": i} for i in ids],
                                      "pagination": {"has_next": has_next}}}


def test_reads_every_page(monkeypatch):
    pages = {"/api/data/collections": page_body([f"c{i}" for i in range(20)], True),
             "/api/data/collections?page=1": page_body(["c20", "c21"], False)}
    calls = []
    monkeypatch.setattr(devclient, "must_get", lambda path, attempts=3: calls.append(path) or pages[path])
    got = [c["id"] for c in devclient.list_collections()]
    assert got == [f"c{i}" for i in range(22)]
    assert calls == ["/api/data/collections", "/api/data/collections?page=1"]


def test_unpaged_list_is_read_once(monkeypatch):
    calls = []
    monkeypatch.setattr(devclient, "must_get", lambda path, attempts=3: calls.append(path) or [{"id": "a"}])
    assert [c["id"] for c in devclient.list_collections()] == ["a"]
    assert calls == ["/api/data/collections"]


def test_a_page_that_repeats_ends_the_loop(monkeypatch):
    monkeypatch.setattr(devclient, "must_get", lambda path, attempts=3: page_body(["x"], True))
    assert [c["id"] for c in devclient.list_collections()] == ["x"]
