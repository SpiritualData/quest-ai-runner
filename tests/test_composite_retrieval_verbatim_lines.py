"""list_sources()/list_operations() must emit each merged line VERBATIM -- offline, no fixtures.

Regression coverage for the bug where CompositeRetrievalAdapter split a line on its first ":" into
(name, description) purely to DEDUPE across adapters, then REBUILT the line as f"{name}: {desc}".
That rebuild corrupted any line whose first colon fell inside its own content rather than at the
intended name/description boundary -- e.g. a line that is itself an example call containing a URL,
such as ``read_section("https://example.com/page")``: the first colon in the whole string is the
one in "https:", so the old code split there, treated 'read_section("https' as the "name", and
rejoined it as ``read_section("https: //example.com/page")`` -- a real space inserted after the
scheme that was never there in the source line. The fix keeps the ORIGINAL line untouched; only the
text before the first colon is used as the dedup key.
"""
from __future__ import annotations

from quest_ai_runner.adapters.composite_retrieval_adapter import CompositeRetrievalAdapter
from quest_ai_runner.core.adapters import Observation


class _StubSourceAdapter:
    """Minimal fake adapter: only implements the one method CompositeRetrievalAdapter calls."""

    def __init__(self, sources_text: str = "", operations_text: str = ""):
        self._sources_text = sources_text
        self._operations_text = operations_text

    def list_sources(self) -> Observation:
        if not self._sources_text:
            return Observation(kind="error", error="no sources")
        return Observation(kind="query", text=self._sources_text)

    def list_operations(self) -> Observation:
        if not self._operations_text:
            return Observation(kind="error", error="no operations")
        return Observation(kind="query", text=self._operations_text)


def _composite(**kwargs) -> CompositeRetrievalAdapter:
    return CompositeRetrievalAdapter([_StubSourceAdapter(**kwargs)])


# --------------------------------------------------------------------------- #
# The exact corruption case: a URL sits right after the line's first colon
# --------------------------------------------------------------------------- #

def test_list_operations_preserves_a_url_containing_line_byte_identical():
    line = 'read_section("https://example.com/page"): fetch a specific page'
    obs = _composite(operations_text=line).list_operations()
    assert obs.kind == "query"
    assert obs.text == line
    # The historical corruption inserted a space after "https:" -- assert it never appears.
    assert "https: //" not in obs.text


def test_list_sources_preserves_a_url_containing_line_byte_identical():
    line = 'docs: see read_section("https://example.com/a:b") for details'
    obs = _composite(sources_text=line).list_sources()
    assert obs.kind == "query"
    assert obs.text == line
    assert "https: //" not in obs.text


def test_list_operations_line_that_is_itself_a_url_call_survives_verbatim():
    # No separate "name: description" framing at all -- the whole line is the example call, so
    # the line's OWN first colon is inside "https:". This is the shape that produced the bug.
    line = 'read_section("https://example.com/page")'
    obs = _composite(operations_text=line).list_operations()
    assert obs.kind == "query"
    assert obs.text == line


# --------------------------------------------------------------------------- #
# Dedup by the text before the line's first colon still happens
# --------------------------------------------------------------------------- #

def test_list_operations_dedupes_by_leading_name_keeping_first_seen_line_verbatim():
    # Two lines share the same leading name ("grep"); only one should survive, and it must be the
    # FIRST one encountered, kept byte-for-byte (not merged/rebuilt from the two).
    text = 'grep: search for a pattern, e.g. grep("https://example.com")\ngrep: an older description'
    obs = _composite(operations_text=text).list_operations()
    assert obs.kind == "query"
    lines = obs.text.split("\n")
    assert len(lines) == 1
    assert lines[0] == 'grep: search for a pattern, e.g. grep("https://example.com")'


def test_list_sources_merges_and_dedupes_across_multiple_adapters():
    a = _StubSourceAdapter(sources_text="files: local corpus files")
    b = _StubSourceAdapter(sources_text='web: fetch pages, e.g. read_section("https://x.com")')
    composite = CompositeRetrievalAdapter([a, b])

    obs = composite.list_sources()
    assert obs.kind == "query"
    lines = set(obs.text.split("\n"))
    assert lines == {
        "files: local corpus files",
        'web: fetch pages, e.g. read_section("https://x.com")',
    }


def test_list_sources_returns_error_when_no_adapter_has_sources():
    obs = _composite().list_sources()
    assert obs.kind == "error"


def test_list_operations_returns_error_when_no_adapter_has_operations():
    obs = _composite().list_operations()
    assert obs.kind == "error"


def test_list_operations_ignores_lines_with_no_colon_and_blank_lines():
    text = "no colon here at all\n\ngrep: search"
    obs = _composite(operations_text=text).list_operations()
    assert obs.kind == "query"
    assert obs.text == "grep: search"
