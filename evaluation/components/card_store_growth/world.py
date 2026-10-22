"""A world of nested card stores, a random sequence of things that happen to it, and what must stay true.

One harness, two users: ``tests/test_card_store_never_inflates.py`` pins it, and the ``card_store_growth``
evaluation arm measures it. It drives the PUBLIC surface of ``FileContextStore`` only (bootstrap, refresh,
record, update, write, prune, merge), with a fake model that never leaves the process, so it also runs
against an older checkout of the library (the arm's "before").

The world: 2 to 5 corpora nested inside one another (``top`` > ``sd`` > ``product`` > ...), each with its
own card store beside it, each holding folders of notes. Options that make it harder: two corpora sharing
one cards folder, a store opened through a symlink, a cards folder that sits inside its own corpus, a
symlink inside a corpus, a store with a small card limit.

The fake model answers with one topic per folder holding two or more notes, as the folders are NOW; ids
and keywords overlap between corpora on purpose (two corpora both have a ``chat`` folder), so cards for
one store's folder cluster with cards imported from another.

After EVERY operation:

  I1  no card id repeats a namespace segment, and none is a vector point id (``card:...``)
  I2  an id has at most as many ``--`` as the world has nesting levels
  I3  no two derived cards of a store pin exactly the same files
  I4  a store that has just been indexed is a fixed point (indexing it again changes nothing), and at
      the end the whole world settles in two rounds
  I5  the settled total of the world never rises by more than the files, folders and cards the
      operations added since the last check (times the stores that can hold a copy)
  I6  no store holds more cards than its limit, unless everything it holds was learned or written for a
      quest (those are admitted whatever the limit)
"""
from __future__ import annotations

import contextlib
import inspect
import json
import logging
import os
import random
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional
from unittest.mock import patch

from quest_ai_runner.adapters.card_repository import FilesystemCardRepository
from quest_ai_runner.adapters.file_context_store import FileContextStore

MERGE_RENAME = re.compile(r"--m[0-9a-f]{8}$")      # ``merge-into`` renames a colliding card <id>--m<8 hex>
VOCAB = ["auth", "billing", "chat", "docs", "export", "search"]
LEVEL_NAMES = ["sd", "product", "runner", "core", "lane"]
NOTES = ["fix the chat bug", "add billing export", "explain search", "document the auth flow"]

OPERATIONS = (["bootstrap"] * 22 + ["refresh"] * 10 + ["bootstrap_nollm"] * 3 + ["edit"] * 10
              + ["add_folder"] * 7 + ["add_file"] * 6 + ["del_file"] * 4 + ["del_folder"] * 3
              + ["record"] * 5 + ["record_files"] * 2 + ["update"] * 5 + ["managed"] * 2 + ["prune"] * 2
              + ["merge"] * 2 + ["widen"] * 2)
SETTLE_EVERY = 8


@contextlib.contextmanager
def offline_git() -> Iterator[None]:
    """No ``git hash-object`` subprocess per file: the same cards, a few times faster."""
    with patch("quest_ai_runner.adapters.context_store_bootstrap.subprocess.run", side_effect=FileNotFoundError):
        yield


def store_accepts(name: str) -> bool:
    """Whether this checkout's ``FileContextStore`` has the constructor argument ``name`` (the arm also runs on older code)."""
    return name in inspect.signature(FileContextStore.__init__).parameters


def runner_self_heal() -> bool:
    """The ``self_heal`` the runner's own builder passes to its ``FileContextStore``, read from the source of
    ``config_assembler.py``, so the arms measure the setting that ships and follow it when it changes. False when
    the builder passes none (the store's default) or the value is not a plain constant."""
    import ast
    try:
        from quest_ai_runner import config_assembler
        tree = ast.parse(Path(inspect.getsourcefile(config_assembler)).read_text(encoding="utf-8"))
    except (ImportError, OSError, SyntaxError, TypeError):      # an older checkout without that builder module
        return False
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", getattr(node.func, "attr", "")) == "FileContextStore":
            for kw in node.keywords:
                if kw.arg == "self_heal" and isinstance(kw.value, ast.Constant):
                    return bool(kw.value.value)
    return False


class DiskProvider:
    """A fake model whose topics follow the files on disk: one topic per folder holding two or more notes."""

    def __init__(self, root: Path):
        self.root = root

    def list_models(self) -> list:
        return []

    def answer(self, messages: Any, model: Any = None, **kw: Any) -> str:
        topics = []
        for folder in sorted(p for p in [self.root, *self.root.rglob("*")] if p.is_dir()):
            parts = folder.relative_to(self.root).parts
            if any(part.startswith(".") for part in parts):
                continue
            files = sorted(f.relative_to(self.root).as_posix() for f in folder.glob("*.md"))
            if len(files) < 2:
                continue
            name = "-".join(parts) or "root"
            word = folder.name if folder != self.root else "root"
            topics.append({"id": f"{name}-topic", "name": f"{name} topic",
                           "keywords": [word, "module", f"{word}-code"],
                           "summary": f"Covers {name}.", "files": files})
        return json.dumps(topics)


def cards_in(cards_dir: Path) -> Dict[str, Dict[str, Any]]:
    return FilesystemCardRepository(str(cards_dir)).load_all()


def file_set(card: Dict[str, Any]) -> frozenset:
    return frozenset((f["path"] if isinstance(f, dict) else f) for f in card.get("files", []))


def is_derived(card: Dict[str, Any]) -> bool:
    """A topic card or a copy of one: what the corpus can regenerate. Everything else was learned or written for a quest."""
    prov = card.get("provenance") if isinstance(card.get("provenance"), dict) else {}
    return prov.get("created_by_task") == "bootstrap" and not card.get("managed_by")


class World:
    """The corpora, their stores, and the operations (all seeded; nothing here is random without ``rng``)."""

    def __init__(self, base: Path, rng: random.Random, *, depth: int, shared: bool = False, symlink: bool = False,
                 cards_below: bool = False, tree_link: bool = False, limit: Optional[int] = None):
        self.base, self.rng, self.limit = base, rng, limit
        self.roots = [base / "top"]
        for i in range(1, depth):
            self.roots.append(self.roots[-1] / LEVEL_NAMES[i - 1])
        self.cards = [r / ".quest-context" for r in self.roots]
        self.shared = shared and depth > 1
        if self.shared:
            self.cards[1] = self.cards[0]
        self.cards_below = cards_below and depth > 1
        if self.cards_below:
            self.cards[0] = self.roots[0] / "docs_home" / ".quest-context"
        self.widened: Dict[int, int] = {}
        self.added = 0           # folders, files and cards the operations have added (for I5)
        self.trace = [f"world depth={depth} shared={shared} symlink={symlink} cards_below={cards_below} "
                      f"tree_link={tree_link} limit={limit}"]
        for i, root in enumerate(self.roots):
            root.mkdir(parents=True, exist_ok=True)
            for _ in range(rng.randint(2, 3)):
                self.add_folder(i)
        self.alias = None
        if symlink:
            self.alias = base / "alias"
            self.alias.symlink_to(self.roots[rng.randrange(depth)])
        if tree_link:
            (self.roots[0] / "shortcut").symlink_to(self.roots[-1], target_is_directory=True)

    # -- the stores -------------------------------------------------------------------------------

    def levels(self) -> range:
        return range(len(self.roots))

    def root_for(self, i: int) -> Path:
        return self.roots[self.widened.get(i, i)]

    def store(self, i: int) -> FileContextStore:
        root = self.root_for(i)
        root_s, cdir = str(root), str(self.cards[i])
        if self.alias is not None and self.alias.resolve() == root.resolve():
            root_s = str(self.alias)
            if self.cards[i] == self.roots[i] / ".quest-context":
                cdir = str(self.alias / ".quest-context")
        kwargs: Dict[str, Any] = dict(repo_root=root_s, auto_bootstrap=False, mount_mode="copy")
        if store_accepts("max_cards"):
            kwargs["max_cards"] = self.limit or 0
        if store_accepts("self_heal"):
            kwargs["self_heal"] = runner_self_heal()     # as the runner ships it, not forced on
        return FileContextStore(cdir, **kwargs)

    def index(self, i: int, *, refresh: bool = False, model: bool = True) -> int:
        root = self.root_for(i)
        store = self.store(i)
        provider = DiskProvider(root.resolve()) if model else None
        # A runner refreshes a store that has a completed bootstrap and bootstraps one that has none
        # (``config_context_index._bootstrap_if_needed``): a store with no ``bootstrap_meta.json`` is not a
        # store to its neighbours, and a refresh never writes one.
        refresh = refresh and (self.cards[i] / "bootstrap_meta.json").exists()
        call = store.refresh_stale if refresh else store.bootstrap
        return call(root=str(root), provider=provider)

    # -- things that happen to the files ------------------------------------------------------------

    def add_folder(self, i: int, count: bool = True) -> None:
        name = self.rng.choice(VOCAB)
        if self.rng.random() < 0.25:
            name = f"{name}/sub{self.rng.randint(0, 2)}"
        folder = self.roots[i] / name
        folder.mkdir(parents=True, exist_ok=True)
        existing = len(list(folder.glob("*.md")))
        for j in range(self.rng.randint(2, 3)):
            (folder / f"n{existing + j}_{self.rng.randint(0, 99)}.md").write_text(
                f"# {name}\nbody {self.rng.random()}\n")
            if count:
                self.added += 1

    def folders(self, i: int) -> List[Path]:
        return [p for p in self.roots[i].rglob("*")
                if p.is_dir() and not p.is_symlink() and ".quest-context" not in p.parts and any(p.glob("*.md"))]

    def mutate(self, i: int, kind: str) -> None:
        notes = [f for d in self.folders(i) for f in d.glob("*.md")]
        if kind == "edit" and notes:
            self.rng.choice(notes).write_text(f"# edited\nbody {self.rng.random()}\n")
        elif kind == "add_file" and notes:
            (self.rng.choice(self.folders(i)) / f"x{self.rng.randint(0, 9999)}.md").write_text("# new\n")
            self.added += 1
        elif kind == "del_file" and notes:
            self.rng.choice(notes).unlink()
        elif kind == "del_folder" and self.folders(i):
            shutil.rmtree(self.rng.choice(self.folders(i)), ignore_errors=True)
        elif kind == "add_folder":
            self.add_folder(i)

    # -- one random operation ------------------------------------------------------------------------

    def apply(self, op: str, i: int) -> None:
        """Do ``op`` at level ``i``. Anything it raises is for the caller to report."""
        from quest_ai_runner.adapters import card_store_identity as ident
        from quest_ai_runner.adapters.card_store_merge import apply_merge
        if op == "bootstrap":
            self.index(i)
        elif op == "refresh":
            self.index(i, refresh=True)
        elif op == "bootstrap_nollm":
            self.index(i, model=False)
        elif op in ("edit", "add_folder", "add_file", "del_file", "del_folder"):
            self.mutate(i, op)
        elif op in ("record", "record_files"):
            files = []
            if op == "record_files":
                notes = [f.relative_to(self.root_for(i)).as_posix() for d in self.folders(i) for f in d.glob("*.md")]
                files = self.rng.sample(notes, min(2, len(notes)))
            self.store(i).record(self.rng.choice(NOTES), {"kind": "met", "files": files})
            self.added += 1
        elif op == "update":
            ids = sorted(cards_in(self.cards[i]))
            if ids:
                card_id = self.rng.choice(ids)
                if self.rng.random() < 0.5:
                    card_id = "card:" + card_id      # how a vector hit names a card
                self.store(i).add_content(card_id, {"type": "note", "locator": {"text": "learned"}, "why": "w"})
                self.added += 1
        elif op == "managed":
            quest = f"quest-{self.rng.randint(0, 9)}"
            self.store(i).write_card(quest, {"id": quest, "name": "quest", "summary": "a quest",
                                             "keywords": ["quest"], "files": [], "managed_by": "quest-sync"})
            self.added += 1
        elif op == "prune":
            self.store(i).prune_only(root=str(self.root_for(i)))
        elif op == "merge" and i + 1 < len(self.roots) and self.cards[i] != self.cards[i + 1] \
                and not self.cards_below:
            narrow, wide = self.cards[i + 1], self.cards[i]
            if ident.read_meta(str(narrow)) and ident.read_meta(str(wide)):
                ident.ensure_store_id(str(narrow))
                ident.ensure_store_id(str(wide))
                extra = {"max_cards": self.limit or 0} if "max_cards" in inspect.signature(apply_merge).parameters else {}
                apply_merge(str(narrow), str(wide), prune_source_after=True, **extra)
        elif op == "widen" and i > 0 and not self.shared:
            self.widened[i] = i - 1          # the lane's root is widened; its cards folder is not moved

    def total(self) -> int:
        return sum(len(cards_in(c)) for c in dict.fromkeys(self.cards))


# -- the invariants ---------------------------------------------------------------------------------

def check(world: World) -> List[str]:
    """I1, I2, I3 and I6 for every store, as they stand now. Empty when all hold."""
    bad: List[str] = []
    depth = len(world.roots)
    for cdir in dict.fromkeys(world.cards):
        cards = cards_in(cdir)
        name = cdir.parent.name
        sets: Dict[frozenset, List[str]] = {}
        for cid, card in cards.items():
            segments = [s for s in cid.split("--") if s]
            if len(set(segments)) != len(segments):
                bad.append(f"I1 {name}: {cid[:90]} repeats a namespace segment")
            if cid.startswith("card:"):
                bad.append(f"I1 {name}: {cid[:90]} is a vector point id")
            if MERGE_RENAME.sub("", cid).count("--") > depth - 1:       # a merge's own rename is not a hop
                bad.append(f"I2 {name}: {cid[:90]} has more prefixes than the world has levels")
            files = file_set(card)
            if files and is_derived(card):       # a card learned from a run may pin what a topic card pins
                sets.setdefault(files, []).append(cid)
        for ids in sets.values():
            if len(ids) > 1:
                bad.append(f"I3 {name}: {' | '.join(i[:50] for i in ids[:3])} pin the same files")
        if world.limit and len(cards) > world.limit and any(is_derived(c) for c in cards.values()):
            bad.append(f"I6 {name}: {len(cards)} cards, over the limit of {world.limit}")
    return bad


def snapshot(world: World) -> Dict[str, List[str]]:
    return {str(c.relative_to(world.base)): sorted(cards_in(c)) for c in dict.fromkeys(world.cards)}


def settled(world: World, scratch: Path) -> Dict[str, Any]:
    """Copy the world, index every store in both orders twice, and say what the two rounds left (I4, I5)."""
    copy = scratch / "settle"
    shutil.rmtree(copy, ignore_errors=True)
    shutil.copytree(world.base, copy, symlinks=True)
    twin = World.__new__(World)
    twin.__dict__.update(world.__dict__)
    twin.base = copy
    twin.roots = [copy / r.relative_to(world.base) for r in world.roots]
    twin.cards = [copy / c.relative_to(world.base) for c in world.cards]
    twin.alias = copy / "alias" if world.alias is not None else None
    twin.trace = []
    rounds = []
    for _ in range(2):
        for i in list(world.levels()) + list(reversed(world.levels())):
            twin.index(i)
        rounds.append(snapshot(twin))
    total = sum(len(ids) for ids in rounds[1].values())
    return {"first": rounds[0], "second": rounds[1], "total": total, "world": twin}


def run(seed: int, steps: int = 24, keep: bool = False) -> Dict[str, Any]:
    """Play one seeded sequence. ``violations`` is empty when every invariant held after every operation."""
    rng = random.Random(seed)
    depth = rng.randint(2, 5)
    options = dict(shared=rng.random() < 0.15, symlink=rng.random() < 0.15, cards_below=rng.random() < 0.15,
                   tree_link=rng.random() < 0.1, limit=rng.choice([None, None, rng.randint(6, 14)]))
    scratch = Path(tempfile.mkdtemp(prefix=f"qar_growth_{seed}_"))
    violations: List[str] = []
    ops_done = 0
    trace: List[str] = []
    try:
        with offline_git():
            logging.disable(logging.CRITICAL)
            world = World(scratch / "w", rng, depth=depth, **options)
            trace = world.trace
            last_total, last_added = 0, 0
            for step in range(steps):
                op, level = rng.choice(OPERATIONS), rng.randrange(depth)
                world.trace.append(f"{step}: {op}({level})")
                try:
                    world.apply(op, level)
                except Exception as exc:  # noqa: BLE001
                    violations.append(f"step {step} {op}({level}) raised {type(exc).__name__}: {exc}")
                    break
                ops_done += 1
                violations += [f"step {step} after {op}({level}): {v}" for v in check(world)]
                if op in ("bootstrap", "refresh") and not violations:
                    before = snapshot(world)
                    world.index(level, refresh=op == "refresh")
                    after = snapshot(world)
                    if after != before:
                        gained = sorted(set().union(*(set(after[k]) - set(before[k]) for k in after)))
                        lost = sorted(set().union(*(set(before[k]) - set(after[k]) for k in after)))
                        violations.append(f"I4 step {step}: {op}({level}) twice in a row is not a fixed point "
                                          f"(gained {gained[:3]}, lost {lost[:3]})")
                if violations:
                    break
                if (step + 1) % SETTLE_EVERY == 0:
                    result = settled(world, scratch)
                    allowed = last_total + len(world.roots) * (2 * (world.added - last_added) + 2)
                    if result["total"] > allowed:
                        violations.append(f"I5 step {step}: the settled world holds {result['total']} cards, "
                                          f"more than the {allowed} that {world.added - last_added} added "
                                          "files and folders can account for")
                    last_total, last_added = result["total"], world.added
            if not violations:
                result = settled(world, scratch)
                if result["first"] != result["second"]:
                    changed = [k for k in result["first"] if result["first"][k] != result["second"][k]]
                    violations.append("I4 the world is not a fixed point after two rounds: " + ", ".join(
                        f"{k} gained {sorted(set(result['second'][k]) - set(result['first'][k]))[:3]} "
                        f"lost {sorted(set(result['first'][k]) - set(result['second'][k]))[:3]}"
                        for k in changed))
                violations += [f"after settling: {v}" for v in check(result["world"])]
    finally:
        logging.disable(logging.NOTSET)
        if not keep:
            shutil.rmtree(scratch, ignore_errors=True)
    return {"seed": seed, "operations": ops_done, "violations": violations, "trace": trace,
            "scratch": str(scratch) if keep else ""}


# -- stores an older version left behind ---------------------------------------------------------------

def is_copy(card: Dict[str, Any]) -> bool:
    prov = card.get("provenance") if isinstance(card.get("provenance"), dict) else {}
    return bool(prov.get("imported_from") or prov.get("origin_store"))


def plant_legacy_copies(root: Path, with_provenance: bool) -> int:
    """What an older version left behind: copies of this store's cards under stacked prefixes."""
    cards = root / ".quest-context"
    planted = 0
    for path in sorted(cards.glob("*.json")):
        card = json.loads(path.read_text())
        if not card.get("files") or is_copy(card):
            continue
        for chain in (f"sd-aaaaaa--product-bbbbbb--{card['id']}", f"root-cccccc--sd-aaaaaa--root-cccccc--{card['id']}"):
            copy = json.loads(json.dumps(card))
            copy["id"] = chain
            copy["provenance"] = {"created_by_task": "bootstrap"}
            if with_provenance:
                copy["provenance"]["imported_from"] = "sd"
            (cards / f"{chain}.json").write_text(json.dumps(copy))
            planted += 1
    return planted
