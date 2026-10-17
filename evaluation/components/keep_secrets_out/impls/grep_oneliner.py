"""BASELINE: the grep one-liner this repo's CLAUDE.md used to prescribe before the scanner existed, ported to Python:
``qsk_[A-Za-z0-9]|sk-ant-|/home/|@host.(com|org)|[0-9a-f]{24}``. It has no pattern for other vendors' tokens, bearer
tokens or private keys, flags every placeholder (``/home/user``, ``name@example.org``, ``qsk_...``, a 40-hex commit
id contains 24 hex characters) and cannot tell a role mailbox from a person. The floor a scanner must beat.
Zero model calls."""
import fnmatch
import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[4]
ALTERNATIVES = [("quest-key", r"qsk_[A-Za-z0-9]"), ("anthropic-key", r"sk-ant-"), ("machine-path", "/ho" + "me/"),
                ("email", r"@[A-Za-z0-9.-]+\.(?:com|org)"), ("object-id", r"[0-9a-f]{24}")]


def names_in(text):
    return sorted({name for name, rx in ALTERNATIVES if re.search(rx, text)})


def build(meter):  # noqa: ARG001
    def scan(kind, args):
        if kind == "text":
            return {"patterns": names_in("".join(args["parts"]))}
        if kind == "repo":
            listed = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard"], cwd=REPO,
                                    capture_output=True, text=True).stdout.split()
            findings = []
            for rel in sorted(listed):
                if not any(fnmatch.fnmatch(rel, g) for g in args["include"]) or \
                        any(fnmatch.fnmatch(rel, g) for g in args.get("exclude", [])):
                    continue
                try:
                    lines = (REPO / rel).read_text().splitlines()
                except (OSError, UnicodeDecodeError):
                    continue
                findings += [f"{rel}:{n}: {name}" for n, line in enumerate(lines, 1) for name in names_in(line)]
            return {"findings": findings}
        return {}
    return scan
