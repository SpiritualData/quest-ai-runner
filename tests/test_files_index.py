"""Closest-path suggestions on a miss and the top-level map: visible paths only, never a denied one."""
from __future__ import annotations

import json

import pytest

from quest_ai_runner.adapters.files_server import NOT_ALLOWED, FileOperations
from quest_ai_runner.adapters.remote_files_adapter import RemoteFilesAdapter


@pytest.fixture()
def root(tmp_path):
    base = tmp_path / "served"
    for rel, body in {
        "CLAUDE.md": "top rules\n",
        "product/setup/unit-files/README.md": "port 8096\n",
        "product/setup/unit-files/unit.service": "x\n",
        "product/launch_code/app/CLAUDE.md": "app rules\n",
        "notes/plain.txt": "hello\n",
        "private_area/plan.md": "secret plan\n",
        "private_area/CLAUDE.md": "private rules\n",
        "hidden_dir/.dotfile": "x\n",
        ".dotted/CLAUDE.md": "dot rules\n",
        "keys/server.pem": "x\n",
    }.items():
        path = base / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
    return base


@pytest.fixture()
def ops(root):
    return FileOperations(str(root), deny=["private_area"])


def test_a_miss_suggests_the_same_path_under_another_top_folder(ops):
    answer = ops.read("setup/unit-files/README.md")
    assert answer["ok"] is False and answer["error"] == NOT_ALLOWED
    assert answer["suggestions"][0] == "product/setup/unit-files/README.md"


def test_a_missing_file_in_a_real_folder_suggests_the_folder(ops):
    answer = ops.read("setup/unit-files/NOPE.md")
    assert "product/setup/unit-files/" in answer["suggestions"]


def test_a_list_miss_suggests_the_folder(ops):
    assert ops.list_dir("setup/unit-files")["suggestions"][0] == "product/setup/unit-files/"


def test_same_name_elsewhere_is_suggested_and_the_typed_path_itself_is_not(ops):
    answer = ops.read("wrong/CLAUDE.md")
    assert set(answer["suggestions"]) == {"CLAUDE.md", "product/launch_code/app/CLAUDE.md"}
    assert "wrong/CLAUDE.md" not in answer["suggestions"]


def test_a_near_spelling_is_suggested(ops):
    assert "product/setup/unit-files/README.md" in ops.read("product/setup/unit-files/READM.md")["suggestions"]


def test_denied_secret_and_dot_names_never_appear_in_any_suggestion(ops):
    seen = []
    for typed in ["private_area/plan.md", "private_area", "plan.md", "private_area/CLAUDE.md",
                  "keys/server.pem", "server.pem", ".dotted/CLAUDE.md", "dotted", ".dotfile",
                  "hidden_dir/.dotfile", "CLAUDE.md", "unit"]:
        for call in (ops.read, ops.list_dir):
            seen += call(typed).get("suggestions", [])
        seen += ops.find("*", scope=typed).get("suggestions", [])
        seen += ops.search("x", scope=typed).get("suggestions", [])
    blob = json.dumps(seen)
    for forbidden in ("private_area", "plan.md", "server.pem", ".dotted", ".dotfile"):
        assert forbidden not in blob


def test_a_denied_path_and_a_missing_one_answer_identically(ops):
    denied = ops.read("private_area/plan.md")
    missing = ops.read("nowhere_at_all/plan.md")
    assert denied == missing


def test_a_suggestion_is_a_function_of_the_typed_text_only(root):
    fenced = FileOperations(str(root), deny=["private_area"])
    unfenced_other = FileOperations(str(root), deny=["private_area", "notes"])
    assert fenced.read("zzz/README.md")["suggestions"] == unfenced_other.read("zzz/README.md")["suggestions"]


def test_the_index_is_cached_until_the_ttl_passes(root):
    now = [0.0]
    ops = FileOperations(str(root), deny=["private_area"])
    ops.index.clock = lambda: now[0]
    ops.read("x/README.md")
    (root / "product" / "setup" / "unit-files" / "NEW_NAME.md").write_text("n\n")
    assert not ops.read("x/NEW_NAME.md").get("suggestions")
    now[0] = 1000.0
    ops.read("x/NEW_NAME.md")                  # stale: answered from the old index, rebuild starts
    with ops.index.lock:                       # the background rebuild releases the lock when done
        pass
    assert ops.read("x/NEW_NAME.md")["suggestions"] == ["product/setup/unit-files/NEW_NAME.md"]


def test_the_map_lists_visible_top_folders_only(ops):
    text = ops.tree_map()["map"]
    assert "product/: launch_code/, setup/" in text
    assert "(top folder files) CLAUDE.md" in text
    for forbidden in ("private_area", "server.pem", "dotted", ".dotfile"):
        assert forbidden not in text
    assert len(text.splitlines()) <= 60


def test_the_map_is_capped(tmp_path):
    base = tmp_path / "wide"
    for i in range(200):
        (base / f"folder{i:03d}" / "sub").mkdir(parents=True)
    text = FileOperations(str(base)).tree_map()["map"]
    lines = text.splitlines()
    assert len(lines) <= 60 and "more top folders" in lines[-1]


def adapter_over(ops, calls):
    def transport(url, headers, body, timeout):
        op = url.rsplit("/", 1)[-1]
        payload = json.loads(body)
        calls.append(op)
        if op == "map":
            return 200, json.dumps(ops.tree_map()).encode()
        if op == "read":
            return 200, json.dumps(ops.read(payload["path"])).encode()
        return 200, json.dumps({"ok": False, "error": "x"}).encode()
    return RemoteFilesAdapter(alias="box", base_url="http://files.example.test", token="tok" * 8,
                              transport=transport)


def test_the_client_shows_suggestions_with_the_alias_and_caches_the_map(ops):
    RemoteFilesAdapter.MAP_CACHE.clear()
    calls: list = []
    adapter = adapter_over(ops, calls)
    obs = adapter.read_section("box:setup/unit-files/README.md")
    assert obs.kind == "error"
    assert "box:product/setup/unit-files/README.md" in obs.error
    assert "private_area" not in obs.error
    first = adapter.map_text()
    second = adapter_over(ops, calls).map_text()
    assert first == second and "product/: launch_code/, setup/" in first
    assert calls.count("map") == 1


def test_a_server_without_a_map_falls_back_to_the_top_listing_and_is_not_asked_again():
    RemoteFilesAdapter.MAP_CACHE.clear()
    calls: list = []

    def transport(url, headers, body, timeout):
        op = url.rsplit("/", 1)[-1]
        calls.append(op)
        if op == "list":
            entries = [{"name": "a", "type": "dir"}, {"name": "b.md", "type": "file"}]
            return 200, json.dumps({"ok": True, "entries": entries}).encode()
        return 404, b"{}"

    adapter = RemoteFilesAdapter(alias="box", base_url="http://files.example.test", token="tok" * 8,
                                 transport=transport)
    assert adapter.map_text() == "a/, b.md"
    adapter.map_text()
    assert calls == ["map", "list"]
