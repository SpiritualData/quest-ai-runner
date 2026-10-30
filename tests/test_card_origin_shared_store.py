"""Writer origin: a lane only mutates the cards it wrote, however many cards it can READ.

A shared card repository (the Quest card backend) lists every card the lane can read, including other
lanes' and other team members' cards whose paths are relative to THEIR roots. Before origins, the
refresh/prune pass treated all of them as its own: it judged them dead (their files are not under this
lane's root) and deleted them from the shared backend, or rewrote them from this machine's files.
"""
import json
from pathlib import Path

import pytest

from hubofhubs.recall import card_store_identity as ident
from hubofhubs.recall.card_repository import FilesystemCardRepository
from quest_ai_runner.adapters.card_store_upload import plan_upload
from quest_ai_runner.adapters.file_context_store import FileContextStore


class SharedRepo:
    """An in-memory card repository shared by every store handed the same instance."""

    def __init__(self):
        self.cards = {}
        self.deleted = []
        self.writes = []
        self._rev = 0

    def load_all(self):
        return {k: json.loads(json.dumps(v)) for k, v in self.cards.items()}

    def read(self, card_id):
        card = self.cards.get(card_id)
        return json.loads(json.dumps(card)) if card is not None else None

    def write(self, card_id, card):
        self.cards[card_id] = json.loads(json.dumps(card))
        self.writes.append(card_id)
        self._rev += 1
        return True

    def delete(self, card_id):
        self.deleted.append(card_id)
        self._rev += 1
        return self.cards.pop(card_id, None) is not None

    def exists(self, card_id):
        return card_id in self.cards

    def revision(self):
        return self._rev


class StubProvider:
    """Topic extraction finds nothing new; a stale card falls back to a re-pin with fresh hashes."""

    def answer(self, *a, **k):
        return "[]"

    def __getattr__(self, name):
        raise AttributeError(name)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("QAR_RUNNER_ENV_ID", "host-1")
    monkeypatch.delenv("QAR_PRUNE_DEAD_CARDS", raising=False)


def _lane(tmp_path, name, repo):
    root = tmp_path / name
    root.mkdir(exist_ok=True)
    (root / "keep.md").write_text(f"# {name} keep\n")
    store = FileContextStore(str(tmp_path / f"{name}-cards"), repo_root=str(root),
                             auto_bootstrap=False, card_repository=repo)
    return root, store


def _card(cid, path, origin=None, sha="x", env="host-1", **extra):
    card = {"id": cid, "name": cid, "keywords": [cid], "summary": f"about {cid}",
            "files": [{"path": path, "sha256": sha}], "runner_env_id": env, **extra}
    if origin:
        card["origin"] = origin
    return card


def test_a_refresh_by_lane_a_never_touches_lane_b_cards(tmp_path):
    repo = SharedRepo()
    root_a, a = _lane(tmp_path, "a", repo)
    root_b, b = _lane(tmp_path, "b", repo)
    assert a.origin_id() != b.origin_id()
    (root_b / "only_on_b.md").write_text("b only")
    # Lane B's cards, including one whose file does not exist on A's root and one that is stale.
    repo.write("b-live", _card("b-live", "only_on_b.md", origin=b.origin_id()))
    repo.write("b-gone-on-a", _card("b-gone-on-a", "elsewhere/missing.md", origin=b.origin_id()))
    repo.write("b-dead", _card("b-dead", "deleted.md", origin=b.origin_id()))
    # Lane A's own: one dead, one stale, one current.
    (root_a / "stale.md").write_text("changed content")
    repo.write("a-dead", _card("a-dead", "deleted_on_a.md", origin=a.origin_id()))
    repo.write("a-stale", _card("a-stale", "stale.md", origin=a.origin_id(), sha="old"))
    before_b = {cid: json.dumps(repo.cards[cid], sort_keys=True) for cid in ("b-live", "b-gone-on-a", "b-dead")}
    repo.writes.clear()

    a.refresh_stale(root=str(root_a), provider=StubProvider())

    assert "a-dead" not in repo.cards, "lane A's own dead card must still be pruned"
    assert set(before_b) <= set(repo.cards), "a lane B card was deleted by lane A"
    assert {cid: json.dumps(repo.cards[cid], sort_keys=True) for cid in before_b} == before_b
    assert not set(repo.deleted) - {"a-dead"}
    assert "a-stale" in repo.writes and repo.cards["a-stale"]["files"][0]["sha256"] != "old"
    assert repo.cards["a-stale"]["origin"] == a.origin_id()
    assert not (set(repo.writes) & set(before_b))


def test_prune_only_by_lane_a_never_touches_lane_b_cards(tmp_path):
    repo = SharedRepo()
    root_a, a = _lane(tmp_path, "a", repo)
    _, b = _lane(tmp_path, "b", repo)
    repo.write("b-card", _card("b-card", "not/on/a.md", origin=b.origin_id(), keywords=["k"] * 500))
    repo.write("b-echo", _card("b-echo", "not/on/a.md", origin=b.origin_id(), summary="Act as X. Quest outcome: y"))
    repo.write("a-dead", _card("a-dead", "gone.md", origin=a.origin_id()))
    snapshot = json.dumps({k: repo.cards[k] for k in ("b-card", "b-echo")}, sort_keys=True)
    repo.writes.clear()

    removed, healed = a.prune_only(root=str(root_a))

    assert removed == 1 and "a-dead" not in repo.cards
    assert json.dumps({k: repo.cards[k] for k in ("b-card", "b-echo")}, sort_keys=True) == snapshot
    assert repo.deleted == ["a-dead"]


def test_two_lanes_on_one_host_do_not_rewrite_each_other(tmp_path):
    repo = SharedRepo()
    root_a, a = _lane(tmp_path, "a", repo)
    root_b, b = _lane(tmp_path, "b", repo)
    (root_a / "shared.md").write_text("A's version")
    (root_b / "shared.md").write_text("B's version")
    repo.write("on-a", _card("on-a", "shared.md", origin=a.origin_id(), sha="a-old"))
    repo.write("on-b", _card("on-b", "shared.md", origin=b.origin_id(), sha="b-old"))

    a.refresh_stale(root=str(root_a), provider=StubProvider())
    assert repo.cards["on-b"]["files"][0]["sha256"] == "b-old"
    assert repo.cards["on-a"]["files"][0]["sha256"] != "a-old"
    b.refresh_stale(root=str(root_b), provider=StubProvider())
    assert repo.cards["on-b"]["files"][0]["sha256"] not in ("b-old", repo.cards["on-a"]["files"][0]["sha256"])


def test_degenerate_foreign_card_is_not_healed_or_removed(tmp_path):
    repo = SharedRepo()
    root_a, a = _lane(tmp_path, "a", repo)
    repo.write("b-degenerate", {"id": "b-degenerate", "name": "", "summary": "x/y.py", "origin": "someone-else",
                                "files": [{"path": "x/y.py"}]})
    a.refresh_stale(root=str(root_a), provider=StubProvider())
    a.prune_only(root=str(root_a))
    assert "b-degenerate" in repo.cards and repo.deleted == []


def test_reading_still_sees_every_readable_card(tmp_path):
    repo = SharedRepo()
    _, a = _lane(tmp_path, "a", repo)
    repo.write("b-card", _card("b-card", "p.md", origin="another"))
    assert "b-card" in a._load_all()
    assert "b-card" not in a._own_cards()


def test_new_cards_are_stamped_and_never_replace_a_foreign_card_with_the_same_id(tmp_path):
    repo = SharedRepo()
    _, a = _lane(tmp_path, "a", repo)
    repo.write("shared-id", _card("shared-id", "p.md", origin="another"))
    assert a.write_card("mine", {"id": "mine", "name": "mine"}) is True
    assert repo.cards["mine"]["origin"] == a.origin_id()
    a.record("fix the login flow", {"kind": "answer", "files": [], "response": "done"})
    turn_cards = [c for c in repo.cards.values() if c.get("user")]
    assert all(c["origin"] == a.origin_id() for c in turn_cards)


def test_usage_stat_write_on_a_foreign_card_keeps_its_origin(tmp_path):
    repo = SharedRepo()
    _, a = _lane(tmp_path, "a", repo)
    repo.write("b-card", _card("b-card", "p.md", origin="another"))
    legacy = _card("legacy-foreign", "p.md")  # no origin, never ours
    repo.write("legacy-foreign", legacy)
    a._load_all()
    assert a.mark_sources_used("b-card", file_paths=["p.md"]) is True
    assert repo.cards["b-card"]["origin"] == "another"
    assert a.mark_sources_used("legacy-foreign", file_paths=["p.md"]) is True
    assert "origin" not in repo.cards["legacy-foreign"]


def test_origin_is_the_store_id_when_the_lane_has_a_local_identity(tmp_path):
    repo = SharedRepo()
    root, a = _lane(tmp_path, "a", repo)
    cards_dir = tmp_path / "a-cards"
    cards_dir.mkdir(exist_ok=True)
    ident.write_meta(str(cards_dir), {"store_id": "st_abc123", "version": 1})
    fresh = FileContextStore(str(cards_dir), repo_root=str(root), auto_bootstrap=False, card_repository=repo)
    assert fresh.origin_id() == "st_abc123"
    # Cards stamped with the derived id before the identity existed are still ours.
    repo.write("old", _card("old", "keep.md", origin=ident.derived_origin("host-1", str(root))))
    assert "old" in fresh._own_cards()


def test_derived_origin_is_deterministic_and_root_specific(tmp_path):
    one, two = tmp_path / "one", tmp_path / "two"
    one.mkdir(), two.mkdir()
    assert ident.derived_origin("h", str(one)) == ident.derived_origin("h", str(one))
    assert ident.derived_origin("h", str(one)) != ident.derived_origin("h", str(two))
    assert ident.derived_origin("h", str(one)) != ident.derived_origin("g", str(one))


def test_legacy_cards_with_an_injected_repository(tmp_path):
    repo = SharedRepo()
    root, a = _lane(tmp_path, "a", repo)
    repo.write("own", _card("own", "keep.md"))                                  # env ok, file under root
    repo.write("other-env", _card("other-env", "keep.md", env="host-2"))        # other environment
    repo.write("missing", _card("missing", "not_here.md"))                      # path not on this root
    repo.write("escapes", _card("escapes", "../outside.md"))                    # leaves the root
    repo.write("no-files", {"id": "no-files", "name": "n", "runner_env_id": "host-1", "files": []})
    repo.write("partial", {**_card("partial", "keep.md"),
                           "files": [{"path": "keep.md"}, {"path": "not_here.md"}]})
    assert set(a._own_cards()) == {"own"}


def test_local_file_mode_is_unchanged(tmp_path):
    root = tmp_path / "corpus"
    root.mkdir()
    (root / "keep.md").write_text("k")
    cards = tmp_path / "cards"
    store = FileContextStore(str(cards), repo_root=str(root), auto_bootstrap=False)
    repo = FilesystemCardRepository(str(cards))
    repo.write("legacy", {"id": "legacy", "name": "l", "files": [{"path": "gone.md", "sha256": "x"}]})
    repo.write("foreign-origin", {"id": "foreign-origin", "name": "f", "origin": "whoever",
                                  "files": [{"path": "gone2.md", "sha256": "x"}]})
    # Every card of a local store is its own, with or without an origin.
    assert set(store._own_cards()) == {"legacy", "foreign-origin"}
    removed, _ = store.prune_only(root=str(root))
    assert removed == 2
    # And what a local store writes is stamped too.
    assert store.write_card("new", {"id": "new", "name": "n"}) is True
    assert repo.read("new")["origin"] == store.origin_id()


def test_turn_and_conversation_cards_carry_the_origin(tmp_path):
    from quest_ai_runner.adapters.conversation_card_builder import ConversationCardBuilder
    from quest_ai_runner.core.turn_context_store import TurnContextStore
    turns = tmp_path / "turns"
    TurnContextStore(turns_dir=str(turns), origin="st_o").record("hello there friend", {"response": "hi"})
    cards = [json.loads(p.read_text()) for p in turns.glob("*.json")]
    assert cards and all(c["origin"] == "st_o" for c in cards)
    builder = ConversationCardBuilder(str(tmp_path / "conv"), origin="st_o")
    path = builder.write_card({"id": "conv-1"})
    assert json.loads(path.read_text())["origin"] == "st_o"
    plain = ConversationCardBuilder(str(tmp_path / "conv2")).write_card({"id": "c2"})
    assert "origin" not in json.loads(plain.read_text())


def test_context_upload_stamps_the_uploading_stores_origin(tmp_path):
    cards_dir = tmp_path / "proj" / ".quest-context"
    repo = FilesystemCardRepository(str(cards_dir))
    repo.write("c1", {"id": "c1", "name": "one", "files": []})
    repo.write("c2", {"id": "c2", "name": "two", "files": [], "origin": "stale-other"})
    ident.write_meta(str(cards_dir), {"store_id": "st_up1", "version": 1, "root": str(tmp_path / "proj")})
    plan = plan_upload(str(cards_dir), team_id="team_x")
    assert {cid: body["origin"] for cid, body in plan.uploads} == {"c1": "st_up1", "c2": "st_up1"}
    # A store with no identity uploads under the derived id its lane computes for the same root.
    bare = tmp_path / "bare" / ".quest-context"
    FilesystemCardRepository(str(bare)).write("c3", {"id": "c3", "name": "three", "files": []})
    plan = plan_upload(str(bare))
    assert plan.uploads[0][1]["origin"] == ident.derived_origin("host-1", str(tmp_path / "bare"))
    lane = FileContextStore(str(bare), repo_root=str(tmp_path / "bare"), auto_bootstrap=False,
                            card_repository=SharedRepo())
    assert lane.origin_id() == plan.uploads[0][1]["origin"]


# --- legacy cards are corrected once, never left for a later backfill ---------------------------

def test_local_refresh_stamps_the_origin_on_legacy_cards_once_without_changing_embed_text(tmp_path):
    from hubofhubs.recall.card_repository import card_embed_text
    root = tmp_path / "local"
    root.mkdir()
    (root / "keep.md").write_text("# keep\n")
    cards_dir = tmp_path / "local-cards"
    repo = FilesystemCardRepository(str(cards_dir))
    legacy = _card("legacy", "keep.md")
    repo.write("legacy", legacy)
    before_text = card_embed_text(repo.read("legacy"))
    store = FileContextStore(str(cards_dir), repo_root=str(root), auto_bootstrap=False)

    store.refresh_stale(root=str(root), provider=StubProvider())

    stamped = FilesystemCardRepository(str(cards_dir)).read("legacy")
    assert stamped["origin"] == store.origin_id()
    assert card_embed_text(stamped) == before_text, "the stamp must never change what is embedded"


def test_shared_refresh_corrects_provably_own_legacy_cards_and_never_claims_foreign_ones(tmp_path):
    repo = SharedRepo()
    root_a, a = _lane(tmp_path, "a", repo)
    repo.write("mine-legacy", _card("mine-legacy", "keep.md"))                    # this env, file present
    repo.write("other-host", _card("other-host", "keep.md", env="host-2"))        # another machine
    repo.write("not-here", _card("not-here", "elsewhere/missing.md"))             # file not on A's root
    repo.writes.clear()

    a.refresh_stale(root=str(root_a), provider=StubProvider())

    assert repo.cards["mine-legacy"]["origin"] == a.origin_id()
    assert "origin" not in repo.cards["other-host"]
    assert "origin" not in repo.cards["not-here"]
    assert {"other-host", "not-here"}.isdisjoint(repo.writes)


def test_a_dry_run_stamps_nothing(tmp_path):
    repo = SharedRepo()
    root_a, a = _lane(tmp_path, "a", repo)
    a._dry_run = True
    repo.write("mine-legacy", _card("mine-legacy", "keep.md"))
    assert a._backfill_origins(a._own_cards()) == 0
    assert "origin" not in repo.cards["mine-legacy"]
