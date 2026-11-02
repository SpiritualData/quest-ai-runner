"""Parse an environment file in the ``KEY=VALUE`` format a systemd ``EnvironmentFile`` accepts.

Parsed in Python and never evaluated by a shell: ``$VAR``, backticks and ``$(...)`` stay literal
text. Accepted: blank lines, ``#`` and ``;`` comment lines, an optional ``export`` prefix, single
or double quotes around a value (double quotes understand ``\\n``, ``\\t``, ``\\"`` and ``\\\\``),
and a trailing `` # comment`` after an unquoted value. A line that is not ``KEY=VALUE`` is skipped.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, Union

__all__ = ["parse_env_text", "read_env_file"]

KEY_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
DOUBLE_QUOTE_ESCAPES = {"n": "\n", "t": "\t", '"': '"', "\\": "\\"}


def unquote_value(raw: str) -> str:
    """The value text after ``=``: quotes removed, escapes resolved in double quotes."""
    text = raw.strip()
    if text[:1] in ("'", '"'):
        quote = text[0]
        out = []
        index = 1
        while index < len(text):
            char = text[index]
            if char == quote:
                return "".join(out)
            if quote == '"' and char == "\\" and index + 1 < len(text):
                following = text[index + 1]
                out.append(DOUBLE_QUOTE_ESCAPES.get(following, "\\" + following))
                index += 2
                continue
            out.append(char)
            index += 1
        return text[1:]  # no closing quote: keep the text as written, minus the opening quote
    comment = re.search(r"\s#", text)
    return (text[:comment.start()] if comment else text).strip()


def parse_env_text(text: str) -> Dict[str, str]:
    """``{KEY: value}`` for every well-formed line of ``text``; later lines win."""
    values: Dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line[0] in "#;":
            continue
        if line.startswith("export ") or line.startswith("export\t"):
            line = line[7:].lstrip()
        key, separator, raw = line.partition("=")
        key = key.strip()
        if not separator or not KEY_PATTERN.match(key):
            continue
        values[key] = unquote_value(raw)
    return values


def read_env_file(path: Union[str, Path]) -> Dict[str, str]:
    """Parse the file at ``path``; a missing file is an error here (a lane must not start blind)."""
    return parse_env_text(Path(path).read_text(encoding="utf-8"))
