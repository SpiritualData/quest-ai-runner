"""Org-scoped context cards on the lane side: scope selection (``QAR_CARDS_SCOPE``), the repository's
org writes/reads/bulk, ``context upload --org-id`` and ``--adopt-ancestor-copies``.

Offline against the fake card API. Ids are obviously fake.
"""
import json

import pytest

from quest_ai_runner import store_cli
from hubofhubs.recall import card_store_identity as ident
from hubofhubs.recall.card_repository import FilesystemCardRepository
from quest_ai_runner.adapters.card_store_upload import REASON_IMPORTED, plan_upload
from quest_ai_runner.adapters.file_context_store import FileContextStore
from quest_ai_runner.adapters.quest_api_card_repository import (
    CardScopeError, QuestApiCardRepository, quest_card_settings_from_env, select_card_scope)
from tests.quest_card_backend_fake import FakeCardBackend
from tests.test_quest_card_backend_default import card_store, local_vector, resolve  # noqa: F401

SCOPE_ENV = ("QAR_CARDS_SCOPE", "QAR_ORG_ID", "QUEST_ORG_ID", "QAR_TEAM_ID", "QUEST_TEAM_ID")


@pytest.fixture
def backend():
    fake = FakeCardBackend()
    yield fake
    fake.close()


@pytest.fixture(autouse=True)
def clean_scope_env(monkeypatch):
    for name in SCOPE_ENV:
        monkeypatch.delenv(name, raising=False)


def requests_to(backend, method, prefix):
    return [r for r in backend.requests if r[0] == method and r[1].startswith(prefix)]


# --- scope selection ---------------------------------------------------------------------

def test_settings_read_the_scope_and_org_names():
    got = quest_card_settings_from_env({"QAR_CARDS_SCOPE": "org:org_1", "QUEST_ORG_ID": "org_q",
                                        "QAR_ORG_ID": "org_qar"})
    assert got["cards_scope"] == "org:org_1" and got["org_id"] == "org_qar"
    assert quest_card_settings_from_env({"QUEST_ORG_ID": "org_q"})["org_id"] == "org_q"


def test_unset_scope_keeps_the_lanes_team():
    assert select_card_scope({"team_id": "team_a"}) == {"team_id": "team_a", "org_id": ""}
    assert select_card_scope({}) == {"team_id": "", "org_id": ""}


def test_explicit_forms():
    assert select_card_scope({"cards_scope": "private", "team_id": "team_a"}) == {"team_id": "", "org_id": ""}
    assert select_card_scope({"cards_scope": "team:team_b", "team_id": "team_a"}) == {"team_id": "team_b", "org_id": ""}
    assert select_card_scope({"cards_scope": "team", "team_id": "team_a"}) == {"team_id": "team_a", "org_id": ""}
    assert select_card_scope({"cards_scope": "org:org_1", "team_id": "team_a", "org_id": "org_2"}) == {
        "team_id": "team_a", "org_id": "org_1"}                    # the id in the value wins
    assert select_card_scope({"cards_scope": "ORG", "org_id": "org_2"})["org_id"] == "org_2"


def test_bare_org_resolves_from_the_lane_team_when_no_org_is_given():
    seen = []

    def lookup(team):
        seen.append(team)
        return "org_from_team"

    got = select_card_scope({"cards_scope": "org", "team_id": "team_a"}, org_of_team=lookup)
    assert got == {"team_id": "team_a", "org_id": "org_from_team"} and seen == ["team_a"]
    seen.clear()
    select_card_scope({"cards_scope": "org", "team_id": "team_a", "org_id": "org_env"}, org_of_team=lookup)
    assert seen == []                                              # an explicit org is never looked up


@pytest.mark.parametrize("settings", [
    {"cards_scope": "org"},                                        # no org, no team
    {"cards_scope": "org", "team_id": "team_a"},                   # team has no org (lookup returns "")
    {"cards_scope": "team"},                                       # no team anywhere
    {"cards_scope": "galaxy:x"},                                   # unknown form
    {"cards_scope": "private:x"},
])
def test_unresolvable_scopes_fail_loudly(settings):
    with pytest.raises(CardScopeError):
        select_card_scope(settings, org_of_team=lambda team: "")


def test_a_failed_team_lookup_is_a_scope_error():
    def boom(team):
        raise RuntimeError("backend down")

    with pytest.raises(CardScopeError, match="backend down"):
        select_card_scope({"cards_scope": "org", "team_id": "team_a"}, org_of_team=boom)


def test_org_scope_from_env_builds_an_org_lane(tmp_path, monkeypatch, local_vector, backend):  # noqa: F811
    monkeypatch.setenv("QUEST_BASE_URL", backend.url)
    monkeypatch.setenv("QUEST_API_KEY", "k")
    monkeypatch.setenv("QUEST_TEAM_ID", "team_a")
    monkeypatch.setenv("QAR_CARDS_SCOPE", "org:org_1")
    repo = card_store(resolve(tmp_path))._repo
    assert isinstance(repo, QuestApiCardRepository)
    assert repo.org_id == "org_1" and repo.team_id == "team_a"
    assert not requests_to(backend, "GET", "/api/teams/team_")      # explicit org: no lookup


def test_bare_org_resolves_through_the_team_api(tmp_path, monkeypatch, local_vector, backend):  # noqa: F811
    backend.teams["team_a"] = {"team_id": "team_a", "org_id": "org_9"}
    monkeypatch.setenv("QUEST_BASE_URL", backend.url)
    monkeypatch.setenv("QUEST_API_KEY", "k")
    monkeypatch.setenv("QUEST_TEAM_ID", "team_a")
    monkeypatch.setenv("QAR_CARDS_SCOPE", "org")
    assert card_store(resolve(tmp_path))._repo.org_id == "org_9"


def test_bare_org_without_a_resolvable_org_fails_startup(tmp_path, monkeypatch, local_vector, backend):  # noqa: F811
    backend.teams["team_a"] = {"team_id": "team_a", "org_id": None}
    monkeypatch.setenv("QUEST_BASE_URL", backend.url)
    monkeypatch.setenv("QUEST_API_KEY", "k")
    monkeypatch.setenv("QUEST_TEAM_ID", "team_a")
    monkeypatch.setenv("QAR_CARDS_SCOPE", "org")
    with pytest.raises(CardScopeError):
        resolve(tmp_path)


def test_private_and_default_scopes_from_env(tmp_path, monkeypatch, local_vector, backend):  # noqa: F811
    monkeypatch.setenv("QUEST_BASE_URL", backend.url)
    monkeypatch.setenv("QUEST_API_KEY", "k")
    monkeypatch.setenv("QUEST_TEAM_ID", "team_a")
    repo = card_store(resolve(tmp_path))._repo
    assert (repo.team_id, repo.org_id) == ("team_a", "")
    monkeypatch.setenv("QAR_CARDS_SCOPE", "private")
    repo = card_store(resolve(tmp_path))._repo
    assert (repo.team_id, repo.org_id) == ("", "")


# --- repository: org writes carry the org scope -------------------------------------------

def test_writes_carry_the_org_scope_and_no_team(backend):
    repo = QuestApiCardRepository(backend.url, "k", team_id="team_a", org_id="org_1")
    assert repo.write("c1", {"id": "c1", "name": "One"}) is True
    method, path, query, _headers, body = requests_to(backend, "PUT", "/api/cards/c1")[0]
    assert query == {"org_id": "org_1"} and "team_id" not in query
    assert "team_id" not in body and "org_id" not in body
    assert ("org:org_1", "c1") in backend.cards and ("team_a", "c1") not in backend.cards


def test_a_listed_org_card_reads_updates_and_deletes_in_its_org(backend):
    backend.put("shared", {"name": "Shared"}, org_id="org_1")
    backend.put("shared", {"name": "Private twin"})                   # same id, private
    repo = QuestApiCardRepository(backend.url, "k", org_id="org_1")
    cards = repo.load_all()
    assert cards["shared"]["org_id"] == "org_1"                        # the lane's own scope wins the id
    assert repo.write("shared", {**cards["shared"], "name": "Edited"}) is True
    assert backend.cards[("org:org_1", "shared")]["name"] == "Edited"
    assert backend.cards[("", "shared")]["name"] == "Private twin"
    assert repo.read("shared")["name"] == "Edited"
    assert repo.delete("shared") is True
    assert ("org:org_1", "shared") not in backend.cards and ("", "shared") in backend.cards
    deletes = requests_to(backend, "DELETE", "/api/cards/shared")
    assert deletes[0][2] == {"org_id": "org_1"}


def test_a_team_lane_still_writes_team_cards_and_reads_org_cards(backend):
    backend.put("from-org", {"name": "Org card"}, org_id="org_1")
    repo = QuestApiCardRepository(backend.url, "k", team_id="team_a")
    assert repo.write("mine", {"id": "mine", "name": "Mine"}) is True
    assert requests_to(backend, "PUT", "/api/cards/mine")[0][2] == {"team_id": "team_a"}
    listed = repo.load_all()
    assert set(listed) == {"mine", "from-org"} and listed["from-org"]["org_id"] == "org_1"
    # Updating the org card it listed goes back to the org, not into the lane's team.
    repo.write("from-org", {**listed["from-org"], "name": "Edited"})
    assert requests_to(backend, "PUT", "/api/cards/from-org")[0][2] == {"org_id": "org_1"}


def test_read_filters_include_org_ids(backend):
    backend.put("a", {"name": "A"}, org_id="org_1")
    repo = QuestApiCardRepository(backend.url, "k", read_org_ids=["org_1", "org_2"])
    repo.load_all()
    repo.search_cards("A")
    for path in ("/api/cards/stamp", "/api/cards", "/api/cards/search"):
        query = [r for r in backend.requests if r[1] == path][-1][2]
        assert query["org_ids"] == "org_1,org_2" and query["include_private"] == "true"


def test_bulk_write_scope_resolution(backend):
    org_lane = QuestApiCardRepository(backend.url, "k", team_id="team_a", org_id="org_1")
    org_lane.bulk_write([("x", {"name": "X"})])
    assert backend.bulk_batches[-1]["org_id"] == "org_1" and "team_id" not in backend.bulk_batches[-1]
    org_lane.bulk_write([("y", {"name": "Y"})], team_id="", org_id="")  # explicit private
    assert "org_id" not in backend.bulk_batches[-1] and "team_id" not in backend.bulk_batches[-1]
    org_lane.bulk_write([("z", {"name": "Z"})], team_id="team_b", org_id="")
    assert backend.bulk_batches[-1]["team_id"] == "team_b"
    team_lane = QuestApiCardRepository(backend.url, "k", team_id="team_a")
    team_lane.bulk_write([("w", {"name": "W"})])
    assert backend.bulk_batches[-1]["team_id"] == "team_a"


def test_org_of_team_reads_the_team_api(backend):
    backend.teams["team_a"] = {"org_id": "org_5"}
    backend.teams["team_solo"] = {"org_id": None}
    repo = QuestApiCardRepository(backend.url, "k")
    assert repo.org_of_team("team_a") == "org_5" and repo.org_of_team("team_solo") == ""


# --- context upload ------------------------------------------------------------------------

def make_store(tmp_path, cards):
    cards_dir = tmp_path / "proj" / ".quest-context"
    repo = FilesystemCardRepository(str(cards_dir))
    for card in cards:
        repo.write(card["id"], card)
    ident.write_meta(str(cards_dir), {"store_id": "st_org1", "version": 1, "root": str(tmp_path / "proj")})
    return cards_dir


def own(cid):
    return {"id": cid, "name": cid, "files": []}


def imported(cid, source):
    return {"id": cid, "name": cid, "files": [], "provenance": {"imported_from": source, "note": "kept"}}


@pytest.fixture
def credentials(monkeypatch, backend):
    monkeypatch.setenv("QUEST_BASE_URL", backend.url)
    monkeypatch.setenv("QUEST_API_KEY", "k")
    for name in ("QAR_QUEST_API_URL", "QAR_QUEST_API_KEY", "QAR_ENV_ID", "QAR_RUNNER_ENV_ID"):
        monkeypatch.delenv(name, raising=False)


def run_cli(*args):
    return store_cli.run(["context", "upload", *args])


def test_upload_with_org_id_sends_every_card_to_the_org(tmp_path, backend, credentials, capsys):
    cards_dir = make_store(tmp_path, [own("c1"), own("c2")])
    assert run_cli("--cards-dir", str(cards_dir), "--org-id", "org_1") == 0
    assert "org org_1" in capsys.readouterr().out and not backend.bulk_batches        # dry run
    assert run_cli("--cards-dir", str(cards_dir), "--org-id", "org_1", "--apply") == 0
    assert all(b["org_id"] == "org_1" and "team_id" not in b for b in backend.bulk_batches)
    assert set(backend.cards) == {("org:org_1", "c1"), ("org:org_1", "c2")}
    sent = backend.bulk_batches[0]["cards"][0]["card"]
    assert "team_id" not in sent and "org_id" not in sent and sent["origin"] == "st_org1"


def test_org_id_is_exclusive_with_team_id_and_private(tmp_path, credentials):
    cards_dir = make_store(tmp_path, [own("c1")])
    for extra in (["--team-id", "t"], ["--private"]):
        with pytest.raises(SystemExit):
            run_cli("--cards-dir", str(cards_dir), "--org-id", "org_1", *extra)


def test_plan_mixed_cards_with_and_without_the_adopt_flag(tmp_path):
    cards_dir = make_store(tmp_path, [
        own("own-1"), imported("nested-1", "stories/x"), imported("anc-1", ".."), imported("anc-2", "../../up")])
    plain = plan_upload(str(cards_dir), org_id="org_1")
    assert [cid for cid, _ in plain.uploads] == ["own-1"]
    assert sorted(plain.skipped[REASON_IMPORTED]) == ["anc-1", "anc-2", "nested-1"] and not plain.adopted

    adopt = plan_upload(str(cards_dir), org_id="org_1", adopt_ancestor_copies=True)
    assert sorted(cid for cid, _ in adopt.uploads) == ["anc-1", "anc-2", "own-1"]
    assert adopt.skipped[REASON_IMPORTED] == ["nested-1"]              # nested imports stay skipped
    assert sorted(adopt.adopted) == ["anc-1", "anc-2"]
    body = dict(adopt.uploads)["anc-2"]
    assert body["provenance"] == {"note": "kept", "adopted_from": "../../up"}
    assert body["origin"] == "st_org1" and body["id"] == "anc-2"
    assert "adopted from an ancestor store: 2" in adopt.render()
    assert "adopted" not in plain.render()


def test_cli_adopt_flag_uploads_ancestor_copies_only(tmp_path, backend, credentials):
    cards_dir = make_store(tmp_path, [own("own-1"), imported("nested-1", "stories/x"), imported("anc-1", "..")])
    assert run_cli("--cards-dir", str(cards_dir), "--team-id", "team_a", "--apply") == 0
    assert set(backend.cards) == {("team_a", "own-1")}
    assert run_cli("--cards-dir", str(cards_dir), "--team-id", "team_a", "--adopt-ancestor-copies",
                   "--apply") == 0
    assert set(backend.cards) == {("team_a", "own-1"), ("team_a", "anc-1")}
    assert "imported_from" not in json.dumps(backend.cards[("team_a", "anc-1")]["provenance"])


def test_adopted_cards_are_the_stores_own_afterwards(tmp_path):
    cards_dir = make_store(tmp_path, [imported("anc-1", "..")])
    plan = plan_upload(str(cards_dir), team_id="team_a", adopt_ancestor_copies=True)
    body = dict(plan.uploads)["anc-1"]

    class Shared:
        def __init__(self):
            self.cards = {"anc-1": body}

        def load_all(self):
            return dict(self.cards)

        def read(self, cid):
            return self.cards.get(cid)

        def write(self, cid, card):
            self.cards[cid] = card
            return True

        def delete(self, cid):
            return self.cards.pop(cid, None) is not None

        def exists(self, cid):
            return cid in self.cards

        def revision(self):
            return 0

    store = FileContextStore(str(cards_dir), repo_root=str(tmp_path / "proj"), auto_bootstrap=False,
                             card_repository=Shared())
    assert store.origin_id() == "st_org1"
    assert list(store._own_cards()) == ["anc-1"]
