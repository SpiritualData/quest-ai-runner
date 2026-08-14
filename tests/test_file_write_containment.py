"""The WRITE boundary — quest-ai-runner's first way to change a consumer's files.

``FilesWriter`` is the only component in the library that writes into the corpus, so its
containment is a SECURITY boundary and is tested like one: every way out of the root gets its own
case, and every case asserts the file OUTSIDE the root is untouched, not merely that a call
returned False.

The boundary itself is ``files_adapter.resolve_in_tree`` — deliberately the same function the
read adapter uses, so there is one implementation to get right and one to fix. The last two tests
pin that sharing, because the failure mode being guarded against is someone later "simplifying" the
writer with a second, weaker check of its own.

Fully offline; no model, no network.
"""
from __future__ import annotations

import os

import pytest

from quest_ai_runner.adapters.files_adapter import FilesAdapter, resolve_in_tree
from quest_ai_runner.adapters.files_writer import FilesWriter


@pytest.fixture
def tree(tmp_path):
    """A corpus root with a file in it, and a secret OUTSIDE it that must stay untouched."""
    root = tmp_path / "corpus"
    (root / "docs").mkdir(parents=True)
    (root / "docs" / "guide.md").write_text("# Guide\n\noriginal line\n")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "passwd").write_text("root:x:0:0\n")
    return root, outside


@pytest.fixture
def writer(tree, tmp_path):
    root, _ = tree
    return FilesWriter(str(root), backup_dir=str(tmp_path / "backups"))


# --- the ordinary cases the boundary must NOT block ---------------------------------------------

def test_writes_an_existing_file_in_the_tree(writer, tree):
    root, _ = tree
    res = writer.write_file("docs/guide.md", "# Guide\n\nedited line\n")
    assert res.ok and res.created is False
    assert (root / "docs" / "guide.md").read_text() == "# Guide\n\nedited line\n"


def test_creates_a_file_that_does_not_exist_yet(writer, tree):
    """The ordinary edit case. ``Path.resolve()`` is non-strict, so a target that does not exist
    yet still normalizes and still gets contained — it must not be refused for being absent."""
    root, _ = tree
    res = writer.write_file("docs/new/note.md", "fresh\n")
    assert res.ok and res.created is True
    assert (root / "docs" / "new" / "note.md").read_text() == "fresh\n"


def test_allows_an_absolute_path_that_is_inside_the_root(writer, tree):
    root, _ = tree
    res = writer.write_file(str(root / "docs" / "guide.md"), "absolute but inside\n")
    assert res.ok
    assert (root / "docs" / "guide.md").read_text() == "absolute but inside\n"


# --- the ways out of the tree, each of which must be refused ------------------------------------

def test_blocks_a_relative_traversal_out_of_the_root(writer, tree):
    root, outside = tree
    res = writer.write_file("../outside/passwd", "pwned\n")
    assert not res.ok and "outside the writable root" in (res.error or "")
    assert (outside / "passwd").read_text() == "root:x:0:0\n"


def test_blocks_a_deep_traversal(writer, tree):
    _, outside = tree
    res = writer.write_file("docs/../../../../../../etc/passwd", "pwned\n")
    assert not res.ok
    assert (outside / "passwd").read_text() == "root:x:0:0\n"


def test_blocks_an_absolute_path_outside_the_root(writer, tree):
    _, outside = tree
    res = writer.write_file(str(outside / "passwd"), "pwned\n")
    assert not res.ok
    assert (outside / "passwd").read_text() == "root:x:0:0\n"


def test_blocks_a_symlinked_directory_that_escapes_the_root(writer, tree):
    """The case a naive ``..``-stripping check misses entirely.

    ``escape/`` is INSIDE the root and its name contains no traversal at all; it is a symlink whose
    target is outside. Containment holds only because resolution follows the link BEFORE the
    ``relative_to`` test, which is exactly why the shared resolver uses ``Path.resolve()``.
    """
    root, outside = tree
    os.symlink(outside, root / "escape")
    res = writer.write_file("escape/passwd", "pwned\n")
    assert not res.ok
    assert (outside / "passwd").read_text() == "root:x:0:0\n"
    assert not (outside / "passwd").read_text().startswith("pwned")


def test_blocks_a_symlinked_file_that_escapes_the_root(writer, tree):
    root, outside = tree
    os.symlink(outside / "passwd", root / "link.txt")
    res = writer.write_file("link.txt", "pwned\n")
    assert not res.ok
    assert (outside / "passwd").read_text() == "root:x:0:0\n"


@pytest.mark.parametrize("name", [".env", ".env.local", "prod.key", "server.pem",
                                  "my_secret_notes.md", "credentials.json", "password_list.txt"])
def test_refuses_credential_ish_files_even_inside_the_root(writer, tree, name):
    """A write must refuse exactly what a read already refuses. These files are IN the tree; being
    contained is not enough to be writable."""
    root, _ = tree
    (root / name).write_text("SECRET-VALUE\n")
    res = writer.write_file(name, "overwritten\n")
    assert not res.ok
    assert (root / name).read_text() == "SECRET-VALUE\n"
    assert writer.read_file(name) is None


def test_refuses_a_binary_file(writer, tree):
    root, _ = tree
    (root / "image.png").write_bytes(b"\x89PNG\r\n")
    res = writer.write_file("image.png", "not an image")
    assert not res.ok
    assert (root / "image.png").read_bytes() == b"\x89PNG\r\n"


def test_refuses_content_over_the_size_limit(tree, tmp_path):
    root, _ = tree
    small = FilesWriter(str(root), backup_dir=str(tmp_path / "b"), max_write_bytes=32)
    res = small.write_file("docs/guide.md", "x" * 100)
    assert not res.ok and "write limit" in (res.error or "")
    assert (root / "docs" / "guide.md").read_text() == "# Guide\n\noriginal line\n"


def test_refuses_to_create_when_creation_is_disabled(tree, tmp_path):
    root, _ = tree
    strict = FilesWriter(str(root), backup_dir=str(tmp_path / "b"), allow_create=False)
    assert not strict.write_file("docs/brand-new.md", "hi\n").ok
    assert not (root / "docs" / "brand-new.md").exists()
    assert strict.write_file("docs/guide.md", "still fine\n").ok


# --- recoverability -----------------------------------------------------------------------------

def test_previous_content_is_backed_up_before_it_is_replaced(writer, tree, tmp_path):
    res = writer.write_file("docs/guide.md", "replaced\n")
    assert res.ok and res.backup_path
    backup = tmp_path / "backups"
    saved = list(backup.iterdir())
    assert len(saved) == 1
    assert saved[0].read_text() == "# Guide\n\noriginal line\n"


def test_the_backup_lives_outside_the_corpus(writer, tree, tmp_path):
    """A backup inside the tree would be indexed as corpus content and would show up as an
    untracked file in the consumer's own version control."""
    root, _ = tree
    writer.write_file("docs/guide.md", "replaced\n")
    assert not any(p.name.endswith(".bak") for p in root.rglob("*"))
    assert list((tmp_path / "backups").iterdir())


def test_a_backup_that_cannot_be_written_refuses_the_overwrite(tree, tmp_path):
    """A backup that was asked for and could not be made must REFUSE the write, not proceed
    without one: proceeding quietly converts a recoverable edit into a destructive one."""
    root, _ = tree
    blocked = tmp_path / "not-a-dir"
    blocked.write_text("this is a file, so mkdir on it fails")
    writer = FilesWriter(str(root), backup_dir=str(blocked))
    res = writer.write_file("docs/guide.md", "replaced\n")
    assert not res.ok and "backup" in (res.error or "")
    assert (root / "docs" / "guide.md").read_text() == "# Guide\n\noriginal line\n"


def test_creating_a_new_file_needs_no_backup(writer, tree):
    res = writer.write_file("docs/created.md", "new\n")
    assert res.ok and res.backup_path is None


def test_backups_can_be_turned_off_deliberately(tree):
    root, _ = tree
    writer = FilesWriter(str(root), backups_enabled=False)
    res = writer.write_file("docs/guide.md", "replaced\n")
    assert res.ok and res.backup_path is None
    assert (root / "docs" / "guide.md").read_text() == "replaced\n"


# --- one boundary, shared by both sides ---------------------------------------------------------

def test_read_and_write_resolve_through_the_same_function(tree):
    """Two implementations of one security boundary is itself the risk: they drift, and only one
    of them gets the fix. Both sides call ``resolve_in_tree``, and this pins that."""
    root, outside = tree
    os.symlink(outside, root / "escape")
    reader = FilesAdapter(str(root))
    writer = FilesWriter(str(root))
    for candidate in ["../outside/passwd", "escape/passwd", str(outside / "passwd"), ".env"]:
        assert reader._resolve_in_tree(candidate) is None
        assert writer.resolve(candidate) is None
        assert resolve_in_tree(root.resolve(), candidate) is None


def test_resolve_in_tree_returns_the_real_path_for_an_allowed_candidate(tree):
    root, _ = tree
    resolved = resolve_in_tree(root.resolve(), "docs/guide.md")
    assert resolved == (root / "docs" / "guide.md").resolve()
