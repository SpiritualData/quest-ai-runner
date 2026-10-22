"""CURRENT: the tag rule as ``esoi check`` runs it, ``scripts/esoi_tags.py`` (rule Q1, ``read_tags``, ``tag_problems``,
``tag_notes``) and its lookup (``solutions_sharing``). A check case writes its tags file to a scratch folder and reads it
the way the rule reads ``esoi/tags.csv``. Zero model calls."""
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO / "scripts"))

import esoi_tags  # noqa: E402


def build(meter):  # noqa: ARG001
    def tags(kind, args):
        if kind == "lookup":
            return {"found": esoi_tags.solutions_sharing(args["ids"], args["solutions"])}
        if kind != "check":
            raise ValueError(kind)
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "esoi").mkdir()
            (Path(tmp) / "esoi" / "tags.csv").write_text(args["tags_csv"], encoding="utf-8")
            listed = esoi_tags.read_tags(Path(tmp), "esoi")
        return {"problems": esoi_tags.tag_problems(listed, args["solutions"], set(args["problems"])),
                "notes": esoi_tags.tag_notes(listed, args["solutions"])}
    return tags
