"""SEARCH/REPLACE + whole-file edit parsing and content-anchored application.

=============================================================================================
VENDORED CODE — attribution and modification notice (Apache-2.0 §4(b))
=============================================================================================
Source project : Aider (https://github.com/Aider-AI/aider)
Source files   : aider/coders/editblock_coder.py  (the module-level functions, lines ~127-628)
                 aider/coders/wholefile_coder.py  (WholeFileCoder.get_edits, lines ~22-122)
Source commit  : 5dc9490bb35f9729ef2c95d00a19ccd30c26339c (2026-05-22)
Upstream license: Apache License 2.0 (LICENSE.txt in that repository; no NOTICE file upstream)
Vendored on    : 2026-08-13, into quest-ai-runner (also Apache-2.0 — no license conflict)

WHY VENDORED RATHER THAN DEPENDED ON
Aider's edit-application layer has never been published as a separate package, so "use it" means
"copy it". It is worth copying rather than reimplementing because almost every line of it exists
to absorb a specific way real models get the format wrong: the markers are matched with
``{5,9}``-repeat regexes because models miscount the ``<`` and ``=`` characters; the filename is
recovered by walking back up to three lines through fences because models put it in the wrong
place; the matcher tolerates a uniform indent offset because that is the error models actually
make. Aider's own ablation found that removing flexible (content-anchored, non-exact) matching
raised editing errors 9x. An approximation written from scratch would lose exactly that, quietly.

WHAT WAS CHANGED FROM UPSTREAM (this file is a MODIFIED copy)
  1. REMOVED all Aider runtime coupling: the ``EditBlockCoder``/``WholeFileCoder`` classes, the
     ``aider.utils`` / ``aider.dump`` / ``base_coder`` / prompt-class imports, and the
     ``main()`` CLI debug entrypoint. What remains is stdlib-only (``difflib``, ``re``,
     ``pathlib``) and imports nothing from Aider or from quest-ai-runner.
  2. REMOVED ``replace_closest_edit_distance`` and the ``math`` import it needed. It is DEAD CODE
     upstream: ``replace_most_similar_chunk`` has an unconditional ``return`` immediately before
     the call, so it never runs. Copying it would have implied a fuzzy-matching rung that does
     not actually exist. Upstream's real ladder is: exact match, then uniform-indent-drift match,
     then explicit ``...`` elision. That is what this copy does, and nothing more. (The dead
     branch's ``return`` was removed with it, so ``replace_most_similar_chunk`` now simply falls
     off the end returning ``None``, which is what it already effectively did.)
  3. CHANGED ``do_replace`` into ``apply_edit``, which is PURE: upstream's version calls
     ``fname.exists()`` and ``fname.touch()``, i.e. it touches the filesystem directly. In this
     repo every corpus write goes through the ``FileWriter`` adapter (containment, secret-file
     refusal, backup), so no vendored code may reach the filesystem. ``apply_edit`` takes the
     current content (``None`` = the file does not exist) and RETURNS the new content; the caller
     decides whether to write it.
  4. ADDED ``find_whole_file_blocks`` — a port of ``WholeFileCoder.get_edits`` reduced to the
     "update" mode: no live-diff rendering, no ``io``/``root`` coupling, and yielding
     ``(filename, new_content)`` rather than Aider's ``(fname, fname_source, lines)`` triples,
     with the same source-priority de-duplication (a filename read from the line above the fence
     beats one merely "seen" in prose, which beats the sole-candidate fallback).

Everything else — ``prep``, ``perfect_replace``, ``perfect_or_whitespace``,
``replace_most_similar_chunk``, ``try_dotdotdots``, ``replace_part_with_missing_leading_whitespace``,
``match_but_for_leading_whitespace``, ``strip_quoted_wrapping``, ``find_original_update_blocks``,
``strip_filename``, ``find_filename``, ``find_similar_lines``, and the HEAD/DIVIDER/UPDATED
patterns — is kept as close to upstream as possible ON PURPOSE, including its naming and comment
style, so a future upstream fix can be re-synced by diffing rather than re-derived.
=============================================================================================
"""
from __future__ import annotations

import difflib
import re
from difflib import SequenceMatcher
from pathlib import Path
from typing import Iterator, List, Optional, Sequence, Tuple

DEFAULT_FENCE = ("`" * 3, "`" * 3)


# --- content-anchored matching -----------------------------------------------------------------

def prep(content):
    if content and not content.endswith("\n"):
        content += "\n"
    lines = content.splitlines(keepends=True)
    return content, lines


def perfect_or_whitespace(whole_lines, part_lines, replace_lines):
    # Try for a perfect match
    res = perfect_replace(whole_lines, part_lines, replace_lines)
    if res:
        return res

    # Try being flexible about leading whitespace
    res = replace_part_with_missing_leading_whitespace(whole_lines, part_lines, replace_lines)
    if res:
        return res


def perfect_replace(whole_lines, part_lines, replace_lines):
    part_tup = tuple(part_lines)
    part_len = len(part_lines)

    for i in range(len(whole_lines) - part_len + 1):
        whole_tup = tuple(whole_lines[i : i + part_len])
        if part_tup == whole_tup:
            res = whole_lines[:i] + replace_lines + whole_lines[i + part_len :]
            return "".join(res)


def replace_most_similar_chunk(whole, part, replace):
    """Best efforts to find the `part` lines in `whole` and replace them with `replace`"""

    whole, whole_lines = prep(whole)
    part, part_lines = prep(part)
    replace, replace_lines = prep(replace)

    res = perfect_or_whitespace(whole_lines, part_lines, replace_lines)
    if res:
        return res

    # drop leading empty line, GPT sometimes adds them spuriously (issue #25)
    if len(part_lines) > 2 and not part_lines[0].strip():
        skip_blank_line_part_lines = part_lines[1:]
        res = perfect_or_whitespace(whole_lines, skip_blank_line_part_lines, replace_lines)
        if res:
            return res

    # Try to handle when it elides code with ...
    try:
        res = try_dotdotdots(whole, part, replace)
        if res:
            return res
    except ValueError:
        pass

    # NOTE (vendoring change #2): upstream falls through to replace_closest_edit_distance here,
    # behind an unconditional `return` that makes it unreachable. Both are omitted; a chunk that
    # matched none of the rungs above returns None, which the caller treats as "did not apply".
    return None


def try_dotdotdots(whole, part, replace):
    """
    See if the edit block has ... lines.
    If not, return none.

    If yes, try and do a perfect edit with the ... chunks.
    If there's a mismatch or otherwise imperfect edit, raise ValueError.

    If perfect edit succeeds, return the updated whole.
    """

    dots_re = re.compile(r"(^\s*\.\.\.\n)", re.MULTILINE | re.DOTALL)

    part_pieces = re.split(dots_re, part)
    replace_pieces = re.split(dots_re, replace)

    if len(part_pieces) != len(replace_pieces):
        raise ValueError("Unpaired ... in SEARCH/REPLACE block")

    if len(part_pieces) == 1:
        # no dots in this edit block, just return None
        return

    # Compare odd strings in part_pieces and replace_pieces
    all_dots_match = all(part_pieces[i] == replace_pieces[i] for i in range(1, len(part_pieces), 2))

    if not all_dots_match:
        raise ValueError("Unmatched ... in SEARCH/REPLACE block")

    part_pieces = [part_pieces[i] for i in range(0, len(part_pieces), 2)]
    replace_pieces = [replace_pieces[i] for i in range(0, len(replace_pieces), 2)]

    pairs = zip(part_pieces, replace_pieces)
    for part, replace in pairs:
        if not part and not replace:
            continue

        if not part and replace:
            if not whole.endswith("\n"):
                whole += "\n"
            whole += replace
            continue

        if whole.count(part) == 0:
            raise ValueError
        if whole.count(part) > 1:
            raise ValueError

        whole = whole.replace(part, replace, 1)

    return whole


def replace_part_with_missing_leading_whitespace(whole_lines, part_lines, replace_lines):
    # GPT often messes up leading whitespace.
    # It usually does it uniformly across the ORIG and UPD blocks.
    # Either omitting all leading whitespace, or including only some of it.

    # Outdent everything in part_lines and replace_lines by the max fixed amount possible
    leading = [len(p) - len(p.lstrip()) for p in part_lines if p.strip()] + [
        len(p) - len(p.lstrip()) for p in replace_lines if p.strip()
    ]

    if leading and min(leading):
        num_leading = min(leading)
        part_lines = [p[num_leading:] if p.strip() else p for p in part_lines]
        replace_lines = [p[num_leading:] if p.strip() else p for p in replace_lines]

    # can we find an exact match not including the leading whitespace
    num_part_lines = len(part_lines)

    for i in range(len(whole_lines) - num_part_lines + 1):
        add_leading = match_but_for_leading_whitespace(
            whole_lines[i : i + num_part_lines], part_lines
        )

        if add_leading is None:
            continue

        replace_lines = [add_leading + rline if rline.strip() else rline for rline in replace_lines]
        whole_lines = whole_lines[:i] + replace_lines + whole_lines[i + num_part_lines :]
        return "".join(whole_lines)

    return None


def match_but_for_leading_whitespace(whole_lines, part_lines):
    num = len(whole_lines)

    # does the non-whitespace all agree?
    if not all(whole_lines[i].lstrip() == part_lines[i].lstrip() for i in range(num)):
        return

    # are they all offset the same?
    add = set(
        whole_lines[i][: len(whole_lines[i]) - len(part_lines[i])]
        for i in range(num)
        if whole_lines[i].strip()
    )

    if len(add) != 1:
        return

    return add.pop()


def strip_quoted_wrapping(res, fname=None, fence=DEFAULT_FENCE):
    """
    Given an input string which may have extra "wrapping" around it, remove the wrapping.
    For example:

    filename.ext
    ```
    We just want this content
    Not the filename and triple quotes
    ```
    """
    if not res:
        return res

    res = res.splitlines()

    if fname and res[0].strip().endswith(Path(fname).name):
        res = res[1:]

    if res[0].startswith(fence[0]) and res[-1].startswith(fence[1]):
        res = res[1:-1]

    res = "\n".join(res)
    if res and res[-1] != "\n":
        res += "\n"

    return res


def apply_edit(content: Optional[str], before_text: str, after_text: str,
               fence: Optional[Tuple[str, str]] = None,
               fname: Optional[str] = None) -> Optional[str]:
    """PURE replacement for upstream's ``do_replace`` (vendoring change #3).

    Args:
        content: the file's CURRENT text, or None when the file does not exist yet.
        before_text: the block's SEARCH text ("" means create/append rather than replace).
        after_text: the block's REPLACE text.
        fence: the fence pair used to strip any accidental wrapping.
        fname: the target path, used only to strip a filename line the model wrapped the text in.

    Returns:
        The file's NEW full text, or None if the SEARCH text could not be located. Returning None
        (rather than raising, or writing a partial result) is what lets the caller report a precise
        diagnostic and leave the file untouched.
    """
    before_text = strip_quoted_wrapping(before_text, fname, fence or DEFAULT_FENCE)
    after_text = strip_quoted_wrapping(after_text, fname, fence or DEFAULT_FENCE)

    if content is None:
        # Upstream calls fname.touch() here. This copy never touches the filesystem: an empty
        # SEARCH against a nonexistent file means "create it with the REPLACE text", and a
        # non-empty SEARCH against a nonexistent file cannot match, so it fails cleanly.
        if before_text.strip():
            return None
        content = ""

    if not before_text.strip():
        # append to existing file, or start a new file
        return content + after_text

    return replace_most_similar_chunk(content, before_text, after_text)


# --- SEARCH/REPLACE wire format ----------------------------------------------------------------

HEAD = r"^<{5,9} SEARCH>?\s*$"
DIVIDER = r"^={5,9}\s*$"
UPDATED = r"^>{5,9} REPLACE\s*$"

HEAD_ERR = "<<<<<<< SEARCH"
DIVIDER_ERR = "======="
UPDATED_ERR = ">>>>>>> REPLACE"

separators = "|".join([HEAD, DIVIDER, UPDATED])

split_re = re.compile(r"^((?:" + separators + r")[ ]*\n)", re.MULTILINE | re.DOTALL)


missing_filename_err = (
    "Bad/missing filename. The filename must be alone on the line before the opening fence"
    " {fence[0]}"
)

# Always be willing to treat triple-backticks as a fence when searching for filenames
triple_backticks = "`" * 3


def strip_filename(filename, fence):
    filename = filename.strip()

    if filename == "...":
        return

    start_fence = fence[0]
    if filename.startswith(start_fence):
        candidate = filename[len(start_fence) :]
        if candidate and ("." in candidate or "/" in candidate):
            return candidate
        return

    if filename.startswith(triple_backticks):
        candidate = filename[len(triple_backticks) :]
        if candidate and ("." in candidate or "/" in candidate):
            return candidate
        return

    filename = filename.rstrip(":")
    filename = filename.lstrip("#")
    filename = filename.strip()
    filename = filename.strip("`")
    filename = filename.strip("*")

    return filename


def find_original_update_blocks(content, fence=DEFAULT_FENCE, valid_fnames=None):
    lines = content.splitlines(keepends=True)
    i = 0
    current_filename = None

    head_pattern = re.compile(HEAD)
    divider_pattern = re.compile(DIVIDER)
    updated_pattern = re.compile(UPDATED)

    while i < len(lines):
        line = lines[i]

        # Check for shell code blocks
        shell_starts = [
            "```bash",
            "```sh",
            "```shell",
            "```cmd",
            "```batch",
            "```powershell",
            "```ps1",
            "```zsh",
            "```fish",
            "```ksh",
            "```csh",
            "```tcsh",
        ]

        # Check if the next line or the one after that is an editblock
        next_is_editblock = (
            i + 1 < len(lines)
            and head_pattern.match(lines[i + 1].strip())
            or i + 2 < len(lines)
            and head_pattern.match(lines[i + 2].strip())
        )

        if any(line.strip().startswith(start) for start in shell_starts) and not next_is_editblock:
            shell_content = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                shell_content.append(lines[i])
                i += 1
            if i < len(lines) and lines[i].strip().startswith("```"):
                i += 1  # Skip the closing ```

            yield None, "".join(shell_content)
            continue

        # Check for SEARCH/REPLACE blocks
        if head_pattern.match(line.strip()):
            try:
                # if next line after HEAD exists and is DIVIDER, it's a new file
                if i + 1 < len(lines) and divider_pattern.match(lines[i + 1].strip()):
                    filename = find_filename(lines[max(0, i - 3) : i], fence, None)
                else:
                    filename = find_filename(lines[max(0, i - 3) : i], fence, valid_fnames)

                if not filename:
                    if current_filename:
                        filename = current_filename
                    else:
                        raise ValueError(missing_filename_err.format(fence=fence))

                current_filename = filename

                original_text = []
                i += 1
                while i < len(lines) and not divider_pattern.match(lines[i].strip()):
                    original_text.append(lines[i])
                    i += 1

                if i >= len(lines) or not divider_pattern.match(lines[i].strip()):
                    raise ValueError(f"Expected `{DIVIDER_ERR}`")

                updated_text = []
                i += 1
                while i < len(lines) and not (
                    updated_pattern.match(lines[i].strip())
                    or divider_pattern.match(lines[i].strip())
                ):
                    updated_text.append(lines[i])
                    i += 1

                if i >= len(lines) or not (
                    updated_pattern.match(lines[i].strip())
                    or divider_pattern.match(lines[i].strip())
                ):
                    raise ValueError(f"Expected `{UPDATED_ERR}` or `{DIVIDER_ERR}`")

                yield filename, "".join(original_text), "".join(updated_text)

            except ValueError as e:
                processed = "".join(lines[: i + 1])
                err = e.args[0]
                raise ValueError(f"{processed}\n^^^ {err}")

        i += 1


def find_filename(lines, fence, valid_fnames):
    """
    Deepseek Coder v2 has been doing this:


     ```python
    word_count.py
    ```
    ```python
    <<<<<<< SEARCH
    ...

    This is a more flexible search back for filenames.
    """

    if valid_fnames is None:
        valid_fnames = []

    # Go back through the 3 preceding lines
    lines = list(lines)
    lines.reverse()
    lines = lines[:3]

    filenames = []
    for line in lines:
        # If we find a filename, done
        filename = strip_filename(line, fence)
        if filename:
            filenames.append(filename)

        # Only continue as long as we keep seeing fences
        if not line.startswith(fence[0]) and not line.startswith(triple_backticks):
            break

    if not filenames:
        return

    # pick the *best* filename found

    # Check for exact match first
    for fname in filenames:
        if fname in valid_fnames:
            return fname

    # Check for partial match (basename match)
    for fname in filenames:
        for vfn in valid_fnames:
            if fname == Path(vfn).name:
                return vfn

    # Perform fuzzy matching with valid_fnames
    for fname in filenames:
        close_matches = difflib.get_close_matches(fname, valid_fnames, n=1, cutoff=0.8)
        if len(close_matches) == 1:
            return close_matches[0]

    # If no fuzzy match, look for a file w/extension
    for fname in filenames:
        if "." in fname:
            return fname

    if filenames:
        return filenames[0]


def find_similar_lines(search_lines, content_lines, threshold=0.6):
    search_lines = search_lines.splitlines()
    content_lines = content_lines.splitlines()

    if not search_lines or len(content_lines) < len(search_lines):
        return ""

    best_ratio = 0
    best_match = None
    best_match_i = 0

    for i in range(len(content_lines) - len(search_lines) + 1):
        chunk = content_lines[i : i + len(search_lines)]
        ratio = SequenceMatcher(None, search_lines, chunk).ratio()
        if ratio > best_ratio:
            best_ratio = ratio
            best_match = chunk
            best_match_i = i

    if best_ratio < threshold or not best_match:
        return ""

    if best_match[0] == search_lines[0] and best_match[-1] == search_lines[-1]:
        return "\n".join(best_match)

    N = 5
    best_match_end = min(len(content_lines), best_match_i + len(search_lines) + N)
    best_match_i = max(0, best_match_i - N)

    best = content_lines[best_match_i:best_match_end]
    return "\n".join(best)


# --- whole-file wire format (vendoring change #4) ----------------------------------------------

def find_whole_file_blocks(content: str, fence: Tuple[str, str] = DEFAULT_FENCE,
                           valid_fnames: Optional[Sequence[str]] = None
                           ) -> Iterator[Tuple[str, str]]:
    """Parse ``path`` + fenced-full-file responses, yielding ``(filename, new_content)``.

    Ported from ``WholeFileCoder.get_edits`` (mode="update"), reduced to what this repo needs:
    no live-diff rendering, no ``io``/``root``, and no ``fname_source`` in the yielded tuple —
    though the SOURCE PRIORITY it encodes is kept, because it is the interesting part: a filename
    read from the line directly above the fence ("block") is trusted over one merely mentioned in
    surrounding prose ("saw"), which is trusted over the sole-candidate fallback ("chat"), and the
    first source to claim a given file wins.

    Unlike upstream this never raises for a missing filename; an unattributable block is skipped,
    since the caller (a fast-edit runner) reports "no edits applied" and escalates rather than
    aborting a chat turn.
    """
    chat_files: List[str] = list(valid_fnames or [])
    lines = content.splitlines(keepends=True)

    edits: List[Tuple[str, str, List[str]]] = []
    saw_fname: Optional[str] = None
    fname: Optional[str] = None
    fname_source: Optional[str] = None
    new_lines: List[str] = []
    # Upstream RAISES when a fenced block has no recoverable filename. Here an unattributable
    # block is instead consumed and dropped, and this flag is what keeps that from corrupting the
    # rest of the parse: without it the block's CLOSING fence would be read as the OPENING fence
    # of a new block, and the last content line as its filename.
    in_unattributed_block = False

    for i, line in enumerate(lines):
        if line.startswith(fence[0]) or line.startswith(fence[1]):
            if in_unattributed_block:
                in_unattributed_block = False
                continue
            if fname is not None:
                # ending an existing block
                saw_fname = None
                edits.append((fname, fname_source or "block", new_lines))
                fname = None
                fname_source = None
                new_lines = []
                continue

            # fname==None ... starting a new block
            if i > 0:
                fname_source = "block"
                fname = lines[i - 1].strip()
                fname = fname.strip("*")  # handle **filename.py**
                fname = fname.rstrip(":")
                fname = fname.strip("`")
                fname = fname.lstrip("#")
                fname = fname.strip()

                # Issue #1232
                if len(fname) > 250:
                    fname = ""

                # Did the model prepend a bogus dir? It especially likes to include the path/to
                # prefix from the one-shot example in the prompt.
                if fname and fname not in chat_files and Path(fname).name in chat_files:
                    fname = Path(fname).name
            if not fname:  # blank line? or ``` was on first line i==0
                if saw_fname:
                    fname = saw_fname
                    fname_source = "saw"
                elif len(chat_files) == 1:
                    fname = chat_files[0]
                    fname_source = "chat"
                else:
                    fname = None
                    fname_source = None
                    in_unattributed_block = True
        elif in_unattributed_block:
            continue
        elif fname is not None:
            new_lines.append(line)
        else:
            for word in line.strip().split():
                word = word.rstrip(".:,;!")
                for chat_file in chat_files:
                    if word == f"`{chat_file}`":
                        saw_fname = chat_file

    if fname:
        edits.append((fname, fname_source or "block", new_lines))

    seen = set()
    # process from most reliable filename, to least reliable
    for source in ("block", "saw", "chat"):
        for edit_fname, edit_source, edit_lines in edits:
            if edit_source != source or edit_fname in seen:
                continue
            seen.add(edit_fname)
            yield edit_fname, "".join(edit_lines)
