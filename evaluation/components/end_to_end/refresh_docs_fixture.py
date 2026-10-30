"""Regenerate the documentation excerpts of the end-to-end world from a checkout of the product documentation.

    python evaluation/components/end_to_end/refresh_docs_fixture.py --docs <quest-docs checkout>   [--check]

``docs_fixture_spec.json`` lists, per excerpt id, the page and where each part starts and ends (a heading or the first words of a
line); the parts are copied VERBATIM, so every fact a documentation case expects is a fact the documentation states. The result
is the ``docs`` and ``docs_provenance`` keys of ``world.json`` (the rest of the file is untouched). ``--docs`` may point at the
checkout root or at its ``docs`` folder; the commit it was copied from is recorded when the folder is a git checkout. A part that
cannot be found, an excerpt that carries a link or an address (or a name listed in ``QUEST_DOCS_REFUSE``), stops the refresh and names it. Run it
again whenever the documentation changes; ``--check`` exits 1 when ``world.json`` is out of date with the checkout."""
import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SPEC = HERE / "docs_fixture_spec.json"
WORLD = HERE / "world.json"
LINKS = re.compile(r"https?://|@[\w-]+\.", re.I)


def refused(text):
    """A link or an address, or any name listed in ``QUEST_DOCS_REFUSE`` (comma separated), found in ``text``; else None."""
    names = [n.strip() for n in os.environ.get("QUEST_DOCS_REFUSE", "").split(",") if n.strip()]
    found = LINKS.search(text) or (re.search(r"\b(" + "|".join(map(re.escape, names)) + r")\b", text, re.I) if names else None)
    return found.group(0) if found else None


def find(lines, start, after=0):
    for i in range(after, len(lines)):
        if lines[i].startswith(start):
            return i
    raise KeyError(start)


def part_text(lines, part):
    first = find(lines, part["start"])
    if "end" not in part:
        return lines[first].strip()
    return "\n".join(lines[first:find(lines, part["end"], first + 1)]).strip("\n")


def excerpts(root, spec=None):
    out = {}
    for key, entry in (spec or json.loads(SPEC.read_text(encoding="utf-8"))).items():
        lines = (root / entry["path"]).read_text(encoding="utf-8").split("\n")
        try:
            text = "\n\n".join(part_text(lines, p) for p in entry["parts"])
        except KeyError as missing:
            raise SystemExit(f"refresh_docs_fixture: {key}: {entry['path']} has no line starting {missing}")
        found = refused(text)
        if found:
            raise SystemExit(f"refresh_docs_fixture: {key}: the excerpt contains {found!r}; narrow its parts")
        out[key] = {"source": "docs/" + entry["path"], "title": entry["title"], "text": text}
    return out


def commit_of(root):
    done = subprocess.run(["git", "-C", str(root), "rev-parse", "--short", "HEAD"], capture_output=True, text=True)
    dirty = subprocess.run(["git", "-C", str(root), "status", "--porcelain", "--", "docs"], capture_output=True, text=True)
    if done.returncode != 0:
        return "unknown (not a git checkout)"
    return done.stdout.strip() + (" plus uncommitted changes" if dirty.stdout.strip() else "")


def render(world_text, docs, provenance):
    """``world.json`` with its ``docs`` and ``docs_provenance`` keys replaced; every other key stays as written."""
    world = json.loads(world_text)
    cuts = [i for i in (world_text.find(',\n "docs_provenance"'), world_text.find(',\n "docs"')) if i >= 0]
    head = world_text[:min(cuts)] if cuts else world_text.rstrip().rstrip("}").rstrip()
    block = {"docs_provenance": provenance, "docs": docs}
    tail = json.dumps(block, indent=1, ensure_ascii=False)[2:-2].replace("\n", "\n ")
    text = head + ",\n " + tail.lstrip() + "\n}\n"
    json.loads(text)
    assert {k: v for k, v in json.loads(text).items() if k not in block} == {k: v for k, v in world.items() if k not in block}
    return text


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--docs", default=os.environ.get("QUEST_DOCS_DIR"), help="the documentation checkout (or QUEST_DOCS_DIR)")
    ap.add_argument("--check", action="store_true", help="exit 1 when world.json differs from the checkout")
    args = ap.parse_args(argv)
    if not args.docs:
        sys.exit("refresh_docs_fixture: give --docs <checkout> (or set QUEST_DOCS_DIR)")
    root = Path(args.docs).resolve()
    docs_root = root / "docs" if (root / "docs").is_dir() else root
    provenance = {"copied_from": "quest-docs", "commit": commit_of(docs_root), "how": "refresh_docs_fixture.py"}
    old = WORLD.read_text(encoding="utf-8")
    try:
        previous = json.loads(old).get("docs_provenance", {})
    except ValueError:
        previous = {}
    new = render(old, excerpts(docs_root), provenance)
    same_text = json.loads(new)["docs"] == json.loads(old).get("docs")
    if args.check:
        print("fresh" if same_text else "stale: the documentation changed since the fixture was copied")
        return 0 if same_text else 1
    if same_text and previous.get("commit") == provenance["commit"]:
        print("fixture already matches the documentation")
        return 0
    WORLD.write_text(new, encoding="utf-8")
    print(f"wrote {len(json.loads(new)['docs'])} excerpts from commit {provenance['commit']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
