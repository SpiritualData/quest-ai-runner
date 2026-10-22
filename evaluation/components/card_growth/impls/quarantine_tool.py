"""CURRENT: the operator's cleanup, ``scripts/quarantine_cards.py`` run with ``--apply`` and its default reasons
(compounded, excluded, surplus, thin) over the store, exactly as an operator runs it. Cards are moved under
``_quarantine/<reason>/``, never deleted, and the manifest is written outside the store. Zero model calls."""
import contextlib
import importlib.util
import io
import tempfile
from pathlib import Path

import common

TOOL = common.REPO / "scripts" / "quarantine_cards.py"


def load_tool():
    spec = importlib.util.spec_from_file_location("quarantine_cards_tool", TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def build(meter):  # noqa: ARG001
    tool = load_tool()

    def settle(cards_dir):
        manifest = Path(tempfile.mkdtemp(prefix="qar_card_growth_manifest_")) / "manifest.json"
        with contextlib.redirect_stdout(io.StringIO()):
            code = tool.main(["--cards-dir", str(cards_dir), "--apply", "--manifest", str(manifest)])
        if code:
            raise RuntimeError(f"quarantine_cards.py exited {code}")
    return settle
