"""The storage each backend runs on in the conformance set, behind one tiny harness.

A harness owns one scratch storage and hands out ``CardRepository`` objects bound to it. The repository CLASS
comes from the implementation module (``impls/``), which imports the live class; the harness only provisions the
storage and builds the class on it: ``repo()`` the first object, ``restart(repo)`` a second object on the same
storage (the first is closed where the backend needs that, as embedded Qdrant locks its folder). ``cleanup()``
removes the storage. Nothing is faked except the network edge of the Quest API (the in-process fake backend in
``tests/quest_card_backend_fake.py``) and the embedder (a deterministic hash vector, so no model or download is
involved)."""
import hashlib
import shutil
import sys
import tempfile

import common  # noqa: F401  (sets sys.path for the repo)

sys.path.insert(0, str(common.REPO / "tests"))

DIMENSION = 16


def hash_embedder(texts):
    """Same text, same vector; no model. The dimension is ``DIMENSION``."""
    out = []
    for text in texts:
        digest = hashlib.sha256((text or "").encode("utf-8", errors="replace")).digest()
        out.append([((digest[i % len(digest)] + i) % 256) / 255.0 for i in range(DIMENSION)])
    return out


class Harness:
    """Base: ``make(storage)`` is the implementation's factory for a repository on this storage."""

    def __init__(self, make, page=None):  # noqa: ARG002
        self.make = make

    def repo(self):
        return self.make(self)

    def restart(self, repo):  # noqa: ARG002
        return self.repo()

    def cleanup(self):
        pass


class FolderHarness(Harness):
    """A scratch folder (the filesystem store, or the embedded Qdrant folder)."""

    def __init__(self, make, page=None, close_on_restart=False):
        super().__init__(make, page)
        self.root = tempfile.mkdtemp(prefix="qar_persist_cards_")
        self.close_on_restart = close_on_restart

    def restart(self, repo):
        if self.close_on_restart:
            try:
                repo._client.close()
            except Exception:  # noqa: BLE001
                pass
        return self.repo()

    def cleanup(self):
        shutil.rmtree(self.root, ignore_errors=True)


class QuestApiHarness(Harness):
    """The in-process fake of the Quest card API. ``page`` caps the fake's page size, so a listing larger than one
    page must be followed to the end."""

    def __init__(self, make, page=None):
        super().__init__(make, page)
        from quest_card_backend_fake import FakeCardBackend
        self.backend = FakeCardBackend(dimension=DIMENSION)
        if page:
            self.backend.page_size_cap = int(page)

    def cleanup(self):
        self.backend.close()


def on_backend(harness_class, make, **options):
    """A ``persist(kind, args)`` function that runs one scenario on a fresh harness of ``harness_class``."""
    from persist_cards.scenarios import KINDS

    def persist(kind, args):
        harness = harness_class(make, page=args.get("page"), **options)
        try:
            return KINDS[kind](harness, args)
        finally:
            harness.cleanup()
    return persist
