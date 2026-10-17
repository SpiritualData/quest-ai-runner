"""Public-repo hygiene scan: find secrets, real machine paths, object ids and real addresses in text.

A repository that is open source (this one, or a consumer's public mirror) must hold nothing consumer
specific or secret. This module is the check behind that rule: ``scan_text`` finds the patterns in one text,
``scan_repo`` walks the files git knows about (tracked, or untracked and not ignored), and the module runs as a
command that exits 1 on any finding:

    python -m quest_ai_runner.structure.hygiene [--root .] [--staged]

Placeholders are not findings (``qsk_...``, ``/home/user/``, ``name@example.org``, a 40-hex commit id), so
documentation can show the SHAPE of a secret. Findings print the pattern name and a redacted snippet, never the
whole secret. Git history is not scanned; a secret already committed must be rotated, not just removed.
"""
from __future__ import annotations

import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional

PLACEHOLDER_USERS = {"user", "you", "me", "name", "username", "someone", "example", "runner", "alice", "bob", "foo"}
ALLOWED_EMAIL_DOMAINS = ("localhost", "invalid", "test", "anthropic.com", "users.noreply.github.com")
PLACEHOLDER_DOMAIN_LABELS = {"example", "domain", "your-workspace", "your-org", "yourorg", "company", "x", "y", "z"}
ROLE_MAILBOXES = {"security", "conduct", "opensource", "noreply", "no-reply", "support", "info", "contact", "admin"}
MAX_BYTES = 2_000_000

PATTERNS: Dict[str, "re.Pattern[str]"] = {
    "anthropic-key": re.compile(r"sk-ant-[A-Za-z0-9_-]{10,}"),
    "openai-key": re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9]{32,}"),
    "quest-key": re.compile(r"\bqsk_[A-Za-z0-9]{24,}"),
    "aws-key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "github-token": re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}"),
    "bearer-token": re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{24,}"),
    "machine-path": re.compile(r"(?:/home/|/Users/)([A-Za-z0-9_.-]+)/|C:\\Users\\([A-Za-z0-9_.-]+)\\"),
    "object-id": re.compile(r"(?<![0-9A-Za-z_])[0-9a-f]{24}(?![0-9A-Za-z_])"),
    "email": re.compile(r"(?<![\w.+\\-])([A-Za-z0-9][A-Za-z0-9._%+-]*)@((?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,6})\b"),
}
# Patterns that span lines: a private key block with a real-length body (a header over a stub body is a test fixture).
BLOCK_PATTERNS: Dict[str, "re.Pattern[str]"] = {
    "private-key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----(?:[A-Za-z0-9+/=]|\s|\\n){64,}-----END"),
}


@dataclass(frozen=True)
class Finding:
    """One hit: where, which pattern, and a redacted snippet."""
    path: str
    line: int
    pattern: str
    snippet: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.pattern}: {self.snippet}"


def redact(hit: str) -> str:
    """Keep the first few characters so a human can find it, hide the rest."""
    return hit[:6] + "..." if len(hit) > 9 else hit


def is_placeholder(pattern: str, match: "re.Match[str]") -> bool:
    """Whether a pattern hit is a documented placeholder rather than the real thing."""
    if pattern == "machine-path":
        return (match.group(1) or match.group(2) or "").lower() in PLACEHOLDER_USERS
    if pattern == "email":
        local, domain = match.group(1).lower(), match.group(2).lower()
        if local in ROLE_MAILBOXES or any(label in PLACEHOLDER_DOMAIN_LABELS for label in domain.split(".")):
            return True
        return any(domain == d or domain.endswith("." + d) for d in ALLOWED_EMAIL_DOMAINS)
    if pattern == "object-id":
        return len(set(match.group(0))) <= 3   # 000000000000000000000000 and similar fillers
    return False


def scan_text(text: str, path: str = "<text>") -> List[Finding]:
    """Every non-placeholder hit in ``text``, one Finding per hit."""
    found: List[Finding] = []
    for number, line in enumerate(text.splitlines(), 1):
        for name, rx in PATTERNS.items():
            for match in rx.finditer(line):
                if not is_placeholder(name, match):
                    found.append(Finding(path, number, name, redact(match.group(0))))
    for name, rx in BLOCK_PATTERNS.items():
        for match in rx.finditer(text):
            found.append(Finding(path, text.count("\n", 0, match.start()) + 1, name, redact(match.group(0))))
    return found


def repo_files(root: Path, staged: bool = False) -> List[Path]:
    """Files git knows about under ``root``: the staged ones, or tracked plus untracked-not-ignored."""
    cmd = ["git", "diff", "--cached", "--name-only", "--diff-filter=ACM"] if staged else \
        ["git", "ls-files", "--cached", "--others", "--exclude-standard"]
    out = subprocess.run(cmd, cwd=root, capture_output=True, text=True, check=True).stdout
    return sorted(p for p in (root / f for f in set(out.splitlines())) if p.is_file())


def scan_repo(root: Path, paths: Optional[Iterable[Path]] = None, staged: bool = False) -> List[Finding]:
    """Scan ``paths`` (default: every file git knows about under ``root``); binary and huge files are skipped."""
    findings: List[Finding] = []
    for path in (list(paths) if paths is not None else repo_files(root, staged)):
        try:
            if path.stat().st_size > MAX_BYTES:
                continue
            text = path.read_bytes().decode("utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        findings.extend(scan_text(text, str(path.relative_to(root))))
    return findings


def main(argv: Optional[List[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    root = Path(args[args.index("--root") + 1]) if "--root" in args else Path(".")
    findings = scan_repo(root.resolve(), staged="--staged" in args)
    for finding in findings:
        print(finding)
    print(f"hygiene scan: {'FAIL, ' + str(len(findings)) + ' finding(s)' if findings else 'clean'}")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
