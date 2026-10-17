"""Collect test files without running them: ``pytest --collect-only`` once per group of files, in this process, with a
small plugin that counts collected items and collection errors. Shared by the live implementation."""
import contextlib
import io

import pytest


class Counter:
    """Counts what collection found for one file."""

    def __init__(self):
        self.collected = 0
        self.errors = []

    def pytest_collection_modifyitems(self, items):
        self.collected += len(items)

    def pytest_collectreport(self, report):
        if report.failed:
            self.errors.append(str(report.nodeid))


def collect_files(paths, rootdir):
    """(collected, error node ids) for the given test files in one ``pytest --collect-only`` call; never runs a
    test body. One call per group, not per file: starting pytest again for each of 300 files costs minutes."""
    counter = Counter()
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        pytest.main(["--collect-only", "-q", "--continue-on-collection-errors", "-p", "no:cacheprovider", "--rootdir", str(rootdir),
                     *[str(p) for p in paths]], plugins=[counter])
    return counter.collected, counter.errors
