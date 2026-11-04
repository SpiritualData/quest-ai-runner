"""A copy of QAR's records as a small repository of their own, for a test that needs a specification index to read.

The records are a tree: the top intent in ``esoi/`` and a folder of records beside the code of each solution. A test that copies only
``esoi/`` would copy only the top intent, so this copies every folder of the tree (its record files, never its code) to the same place
in the copy and writes the ``esoi.toml`` that lists them.
"""
import shutil
from pathlib import Path

from esoi import intents

REPO = Path(__file__).resolve().parents[1]
RECORD_FILES = ("intent.csv", *intents.RECORD_FILES)


def copy_records(destination: Path) -> None:
    """``destination/esoi`` is the structure folder (without its tracker); every folder beside the code holds its record files."""
    structure = (REPO / "esoi").resolve()
    shutil.copytree(structure, destination / "esoi", ignore=shutil.ignore_patterns("tracker"))
    for folder in intents.tree_folders(structure):
        folder = folder.resolve()
        if folder == structure:
            continue
        target = destination / folder.relative_to(REPO)
        target.mkdir(parents=True, exist_ok=True)
        for name in RECORD_FILES:
            if (folder / name).is_file():
                shutil.copy2(folder / name, target / name)
    (destination / "esoi.toml").write_text(
        'package = "x"\nstructure_dir = "esoi"\n[records]\nbeside_code = ["quest_ai_runner", "quest_ai_runner/*"]\n', encoding="utf-8")
