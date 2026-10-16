"""Secret-VALUE redaction for text the file server hands out.

A net, not a guarantee: keep real secrets out of the served tree. Every pattern is linear (no nested
quantifiers) and anchored on a lookbehind, so running it over a few hundred kilobytes is cheap; the
server still redacts only text that is about to be returned (after truncation to the read cap), never
a whole 20 MB file.
"""
from __future__ import annotations

import re
from typing import Callable, Tuple

REDACTED = "[redacted]"

# Whole-match replacements: a long, key-shaped value with a recognizable prefix.
PREFIXED_VALUE_PATTERNS: Tuple["re.Pattern[str]", ...] = tuple(re.compile(p) for p in (
    r"-----BEGIN [A-Z ]*PRIVATE KEY[A-Z ]*-----[\s\S]*?(?:-----END [A-Z ]*PRIVATE KEY[A-Z ]*-----|\Z)",
    r"(?<![A-Za-z0-9])sk-[A-Za-z0-9_\-]{20,}",                      # OpenAI and Anthropic (sk-ant-)
    r"(?<![A-Za-z0-9])[sr]k_(?:live|test)_[A-Za-z0-9]{10,}",        # Stripe secret and restricted
    r"(?<![A-Za-z0-9])whsec_[A-Za-z0-9+/=]{16,}",                   # Stripe webhook signing
    r"(?<![A-Za-z0-9])qsk_[A-Za-z0-9]{16,}",
    r"(?<![A-Za-z0-9])(?:AKIA|ASIA)[0-9A-Z]{16}(?![A-Za-z0-9])",
    r"(?<![A-Za-z0-9])gh[pousr]_[A-Za-z0-9]{30,}",
    r"(?<![A-Za-z0-9])github_pat_[A-Za-z0-9_]{20,}",
    r"(?<![A-Za-z0-9])xox[abprs]-[A-Za-z0-9\-]{10,}",
    r"(?<![A-Za-z0-9])AIza[0-9A-Za-z_\-]{30,}",
    r"(?<![A-Za-z0-9])eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}",
    r"(?i)(?<![A-Za-z0-9])Bearer\s+[A-Za-z0-9._~+/\-]{16,}=*",
    r"(?i)(?<![A-Za-z0-9])Basic\s+[A-Za-z0-9+/]{16,}={0,2}",
))

# Prefixes short enough to collide with ordinary identifiers: only redacted when the value also
# contains a digit (real keys always do, ``re_compile_pattern_for_search`` does not).
DIGIT_REQUIRED_PATTERNS: Tuple["re.Pattern[str]", ...] = tuple(re.compile(p) for p in (
    r"(?<![A-Za-z0-9_\-])re_[A-Za-z0-9_]{20,}",                     # Resend
    r"(?<![A-Za-z0-9_\-])pa-[A-Za-z0-9_\-]{30,}",                   # Voyage
))

URI_CREDENTIALS = re.compile(
    r"(?<![A-Za-z0-9+.\-])(?P<head>[A-Za-z][A-Za-z0-9+.\-]*://[^\s:/@'\"]+:)(?P<secret>[^\s@/'\"]+)(?P<tail>@)")

# NAME = value, where NAME contains a secret word. ``export FOO_API_KEY=...``, ``"token": "..."``,
# ``password: ...``. Two strengths: the strong words redact any value of 6+ characters, ``key``
# needs 16+ so ``sort_key = name`` survives.
ASSIGNMENT_TEMPLATE = (
    r"(?i)(?<![A-Za-z0-9_\-])(?P<name>[A-Za-z0-9_\-]{{0,64}}(?:{words})[A-Za-z0-9_\-]{{0,40}})"
    r"(?P<sep>[\"']?[ \t]*(?:=>|:=|=|:)[ \t]*)"
    r"(?:(?P<quote>[\"'])(?P<quoted>[^\"'\n]{{{minimum},}}?)(?P=quote)|(?P<bare>[^\s\"'`,;(){{}}<>\[\]]{{{minimum},}}))")
STRONG_ASSIGNMENT = re.compile(ASSIGNMENT_TEMPLATE.format(
    words="secret|token|passw(?:or)?d|pwd|credential", minimum=6))
KEY_ASSIGNMENT = re.compile(ASSIGNMENT_TEMPLATE.format(words="key", minimum=16))

# Command line forms: --password VALUE, --token=VALUE, -p 'VALUE'.
CLI_LONG_FLAG = re.compile(
    r"(?i)(?<![A-Za-z0-9_])(?P<flag>--?(?:password|passwd|pwd|pass|token|secret|api-?key|access-?key))"
    r"(?P<sep>=|[ \t]+)(?:(?P<quote>[\"'])(?P<quoted>[^\"'\n]+)(?P=quote)|(?P<bare>[^\s\"']+))")
CLI_SHORT_P = re.compile(r"(?<![A-Za-z0-9_\-])-p(?P<sep>[ \t]*)(?P<quote>[\"'])(?P<quoted>[^\"'\n]+)(?P=quote)")

BARE_PLACEHOLDERS = frozenset({"none", "null", "true", "false", "undefined", "nil", "changeme",
                               "required", "optional", "string", "secret", "password", "token"})


def looks_like_reference(value: str) -> bool:
    """Values that are not secrets: placeholders, variable references, numbers, prior redactions."""
    low = value.lower()
    return (value.isdigit() or low in BARE_PLACEHOLDERS or value.startswith(("$", "%", "[redacted"))
            or value.startswith("{{"))


def redact_assignment(match: "re.Match[str]") -> str:
    value = match.group("quoted") if match.group("quote") else match.group("bare")
    if looks_like_reference(value) or (match.group("bare") and match.string[match.end():match.end() + 1] == "("):
        return match.group(0)      # a placeholder, a variable, or a function call such as os.getenv(...)
    quote = match.group("quote") or ""
    return f"{match.group('name')}{match.group('sep')}{quote}{REDACTED}{quote}"


def redact_cli(match: "re.Match[str]") -> str:
    value = match.group("quoted") if match.group("quote") else match.group("bare")
    if looks_like_reference(value) or value.startswith("-"):
        return match.group(0)
    quote = match.group("quote") or ""
    head = match.group("flag") if "flag" in match.re.groupindex else "-p"
    return f"{head}{match.group('sep')}{quote}{REDACTED}{quote}"


def digit_gate(match: "re.Match[str]") -> str:
    return REDACTED if any(ch.isdigit() for ch in match.group(0)) else match.group(0)


def redact(text: str) -> str:
    """Replace obvious secret values in ``text`` with ``[redacted]``. Never raises."""
    if not text:
        return text
    out = text
    try:
        for pattern in PREFIXED_VALUE_PATTERNS:
            out = pattern.sub(REDACTED, out)
        for pattern in DIGIT_REQUIRED_PATTERNS:
            out = pattern.sub(digit_gate, out)
        out = URI_CREDENTIALS.sub(lambda m: f"{m.group('head')}{REDACTED}{m.group('tail')}", out)
        out = CLI_LONG_FLAG.sub(redact_cli, out)
        out = CLI_SHORT_P.sub(redact_cli, out)
        out = STRONG_ASSIGNMENT.sub(redact_assignment, out)
        out = KEY_ASSIGNMENT.sub(redact_assignment, out)
    except re.error:  # pragma: no cover - patterns are static
        return REDACTED
    return out


Redactor = Callable[[str], str]
