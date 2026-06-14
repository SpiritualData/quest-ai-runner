"""FileContextStore -- a stdlib-only ContextAssembler backed by per-card JSON files.

Cards are the source of truth: one JSON file per card under a configurable ``cards_dir``.
The store selects relevant cards by keyword overlap with the task text, checks each pinned
file's freshness (sha256 + mtime; git blob SHA when a repo_root is set and git is available),
renders fresh cards into a ``context_view`` string, and flags stale files. ``record()``
upserts a card keyed by a stable slug of the task and re-pins file fingerprints.

No LLM calls, no third-party imports -- stdlib only (json, hashlib, os, re, subprocess,
pathlib, etc.).

Card JSON schema (matches docs/context-assembly.md exactly):
  {
    "id": "subsystem-or-hash",
    "keywords": ["chat", "ai", "conversation"],
    "summary": "what this subsystem is and how it is wired",
    "files": [
      {"path": "rel/path.py", "git_sha": "...", "mtime": 1700000000.0,
       "sha256": "...", "why": "entry point", "symbols": ["run", "execute"]}
    ],
    "conventions": ["pointer to a rule that applies"],
    "provenance": {
      "created_by_task": "...", "model": "...",
      "created_at": "...", "last_verified_at": "..."
    },
    "usage_count": 0,
    "last_outcome": "met|failed|unknown"
  }
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from ..core.adapters import AssembledContext, ContextAssemblerBase

# Short stopwords dropped from keyword tokenization (pure ASCII, lowercase).
_STOPWORDS: Set[str] = {
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from",
    "has", "have", "how", "i", "in", "is", "it", "its", "me", "my",
    "not", "of", "on", "or", "that", "the", "this", "to", "was", "we",
    "what", "when", "where", "which", "who", "will", "with", "you",
}
_MIN_TOKEN_LEN = 3  # tokens shorter than this are always dropped


def _tokenize(text: str) -> Set[str]:
    """Lowercase-tokenize ``text`` to a keyword set, dropping short tokens and stopwords."""
    raw = re.findall(r"[a-z0-9]+", text.lower())
    return {t for t in raw if len(t) >= _MIN_TOKEN_LEN and t not in _STOPWORDS}


def _card_slug(task_text: str) -> str:
    """Stable card id: first few keywords (sorted) + short sha256 digest of the text."""
    tokens = sorted(_tokenize(task_text))
    prefix = "-".join(tokens[:4]) if tokens else "card"
    digest = hashlib.sha256(task_text.encode("utf-8", errors="replace")).hexdigest()[:8]
    return f"{prefix}-{digest}"


class FileContextStore(ContextAssemblerBase):
    """Stdlib-only ContextAssembler backed by per-card JSON files.

    Constructor args:
      cards_dir        -- directory where card JSON files live (created on first write).
      repo_root        -- optional path to a git repo root for git-blob-SHA staleness checks.
                         Best-effort, optional: if git is unavailable the check is skipped.
      max_cards_in_view -- maximum number of cards included in a single assembled context view.
    """

    def __init__(
        self,
        cards_dir: str,
        *,
        repo_root: Optional[str] = None,
        max_cards_in_view: int = 5,
    ) -> None:
        self._cards_dir = Path(cards_dir)
        self._repo_root = Path(repo_root).resolve() if repo_root else None
        self._max_cards = max_cards_in_view

    # ------------------------------------------------------------------
    # Public API: ContextAssemblerBase implementation
    # ------------------------------------------------------------------

    def assemble(
        self, task_text: str, *, meta: Optional[Dict[str, Any]] = None
    ) -> AssembledContext:
        """Select and render relevant cards for ``task_text``. Never raises."""
        try:
            return self._assemble_inner(task_text)
        except Exception:  # noqa: BLE001
            return AssembledContext()

    def record(self, task_text: str, outcome: Dict[str, Any]) -> None:
        """Upsert a card for this task and write it atomically. Never raises."""
        try:
            self._record_inner(task_text, outcome)
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------
    # Public helper: O(1) invalidation index
    # ------------------------------------------------------------------

    def stale_cards_for(self, path: str) -> Set[str]:
        """Return card ids that pin ``path`` AND whose fingerprint for it is now stale.

        The index is rebuilt on each call from the on-disk cards (no in-process cache so
        concurrent writes from other agents are always reflected). Best-effort: returns an
        empty set on any error.
        """
        try:
            card_ids: Set[str] = set()
            for card in self._load_all().values():
                for fe in card.get("files", []):
                    if fe.get("path") == path:
                        fp = self._fingerprint(path)
                        if fp.get("sha256") and fe.get("sha256") != fp["sha256"]:
                            card_ids.add(card["id"])
            return card_ids
        except Exception:  # noqa: BLE001
            return set()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _assemble_inner(self, task_text: str) -> AssembledContext:
        task_kws = _tokenize(task_text)
        if not task_kws:
            return AssembledContext()

        cards = self._load_all()
        if not cards:
            return AssembledContext()

        # Score each card by keyword overlap with the task.
        scored: List[tuple] = []  # (overlap_count, card_dict)
        for card in cards.values():
            card_kws = set(card.get("keywords", []))
            overlap = len(task_kws & card_kws)
            if overlap > 0:
                scored.append((overlap, card))

        if not scored:
            return AssembledContext()

        # Highest-overlap cards first, limited to max_cards_in_view.
        scored.sort(key=lambda x: x[0], reverse=True)
        top_cards = [c for _, c in scored[: self._max_cards]]

        # Render each card, checking file freshness inline.
        view_parts: List[str] = []
        card_ids: List[str] = []
        stale_list: List[str] = []

        for card in top_cards:
            card_id = card.get("id", "")
            card_ids.append(card_id)
            summary = card.get("summary", "(no summary)")
            files = card.get("files", [])

            file_lines: List[str] = []
            for fe in files:
                fpath = fe.get("path", "")
                stored_sha = fe.get("sha256", "")
                current_fp = self._fingerprint(fpath)
                current_sha = current_fp.get("sha256", "")
                changed = (
                    (bool(stored_sha) and bool(current_sha) and current_sha != stored_sha)
                    or (bool(stored_sha) and not current_sha)  # file disappeared
                )
                if changed:
                    stale_list.append(fpath)
                    file_lines.append(f"  - {fpath} (changed since last capture)")
                else:
                    why = fe.get("why", "")
                    syms = fe.get("symbols", [])
                    sym_note = f" [{', '.join(syms[:5])}]" if syms else ""
                    file_lines.append(
                        f"  - {fpath}{sym_note}" + (f"  -- {why}" if why else "")
                    )

            file_block = "\n".join(file_lines) if file_lines else "  (no pinned files)"
            conventions = card.get("conventions", [])
            conv_block = (
                "\n".join(f"  * {c}" for c in conventions[:10]) if conventions else ""
            )
            part = f"### Card: {card_id}\n{summary}\n\nFiles:\n{file_block}"
            if conv_block:
                part += f"\n\nConventions:\n{conv_block}"
            view_parts.append(part)

        context_view = "\n\n---\n\n".join(view_parts)
        return AssembledContext(
            context_view=context_view,
            card_ids=card_ids,
            stale=list(dict.fromkeys(stale_list)),  # deduplicate, preserve order
        )

    def _record_inner(self, task_text: str, outcome: Dict[str, Any]) -> None:
        card_id = _card_slug(task_text)
        card_path = self._cards_dir / f"{card_id}.json"

        # Load existing card or start fresh.
        if card_path.exists():
            try:
                with open(card_path, "r", encoding="utf-8") as fh:
                    card: Dict[str, Any] = json.load(fh)
            except Exception:  # noqa: BLE001 -- corrupt card: start fresh
                card = {}
        else:
            card = {}

        # Ensure required fields exist.
        card.setdefault("id", card_id)
        card.setdefault("keywords", sorted(_tokenize(task_text)))
        card.setdefault("summary", task_text[:200])
        card.setdefault("conventions", [])
        card.setdefault("usage_count", 0)
        card.setdefault("last_outcome", "unknown")
        card.setdefault("provenance", {
            "created_by_task": task_text[:100],
            "model": "",
            "created_at": "",
            "last_verified_at": "",
        })

        # Re-pin file fingerprints when outcome supplies a file list.
        file_paths: List[str] = outcome.get("files") or []
        if file_paths:
            existing_files: Dict[str, Dict[str, Any]] = {
                fe["path"]: fe for fe in card.get("files", []) if "path" in fe
            }
            refreshed: List[Dict[str, Any]] = []
            for fpath in file_paths:
                fp = self._fingerprint(fpath)
                entry = existing_files.get(fpath, {"path": fpath, "why": "", "symbols": []})
                entry = dict(entry)  # copy to avoid mutating the source dict
                entry["path"] = fpath
                entry["sha256"] = fp.get("sha256", "")
                entry["mtime"] = fp.get("mtime", 0.0)
                entry["git_sha"] = fp.get("git_sha", "")
                refreshed.append(entry)
            card["files"] = refreshed
        # else: keep existing file entries unchanged

        # Update usage and outcome.
        card["usage_count"] = card.get("usage_count", 0) + 1
        kind = outcome.get("kind")
        if kind in ("met", "failed", "unknown", "answer", "deep", "confirm"):
            # Normalize runner kind strings to the card schema vocabulary.
            card["last_outcome"] = kind if kind in ("met", "failed", "unknown") else {
                "answer": "met", "deep": "met", "confirm": "unknown"
            }.get(kind, "unknown")

        # Accept an optional timestamp for last_verified_at (avoids datetime.now in tests).
        ts = outcome.get("verified_at") or outcome.get("timestamp")
        if ts:
            card["provenance"]["last_verified_at"] = str(ts)

        # Atomic write via tmp + os.replace.
        self._cards_dir.mkdir(parents=True, exist_ok=True)
        tmp_fd, tmp_path = tempfile.mkstemp(
            dir=str(self._cards_dir), prefix=".tmp_", suffix=".json"
        )
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as fh:
                json.dump(card, fh, indent=2, ensure_ascii=False)
                fh.write("\n")
            os.replace(tmp_path, str(card_path))
        except Exception:  # noqa: BLE001
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise  # re-raise so the outer try/except in record() catches it

    def _fingerprint(self, path: str) -> Dict[str, Any]:
        """Compute current sha256 + mtime + (optional) git_sha for a file path.

        Returns a dict with the fields that exist; missing/unreadable fields are omitted
        or set to empty string. Never raises.
        """
        result: Dict[str, Any] = {"sha256": "", "mtime": 0.0, "git_sha": ""}
        try:
            p = Path(path)
            if not p.exists() or not p.is_file():
                return result
            stat = p.stat()
            result["mtime"] = stat.st_mtime
            h = hashlib.sha256()
            with open(p, "rb") as fh:
                for chunk in iter(lambda: fh.read(65536), b""):
                    h.update(chunk)
            result["sha256"] = h.hexdigest()
        except Exception:  # noqa: BLE001
            pass

        # Git blob SHA: best-effort, optional, fully wrapped.
        if self._repo_root is not None and result["sha256"]:
            try:
                proc = subprocess.run(
                    ["git", "-C", str(self._repo_root), "hash-object", path],
                    capture_output=True, text=True, timeout=5,
                )
                git_sha = proc.stdout.strip()
                if git_sha:
                    result["git_sha"] = git_sha
            except Exception:  # noqa: BLE001 -- git unavailable, no repo, any error: skip
                pass

        return result

    def _load_all(self) -> Dict[str, Dict[str, Any]]:
        """Load all card JSON files from cards_dir. Returns {card_id: card_dict}."""
        if not self._cards_dir.exists():
            return {}
        cards: Dict[str, Dict[str, Any]] = {}
        for entry in self._cards_dir.iterdir():
            if entry.suffix != ".json" or entry.name.startswith("."):
                continue
            try:
                with open(entry, "r", encoding="utf-8") as fh:
                    card = json.load(fh)
                card_id = card.get("id") or entry.stem
                cards[card_id] = card
            except Exception:  # noqa: BLE001 -- corrupt card: skip
                continue
        return cards
