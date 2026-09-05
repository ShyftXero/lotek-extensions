"""One-predicate-one-home guard for un-adopt (#635).

"Is this finding enriched by job J" is decided in ONE function — `promote.finding_is_enriched` — reached
through ONE caller (`promote.enriched_findings`). The destructive-un-adopt PREVIEW and DELETE both go
through that caller, so "what the preview lists" and "what the delete removes" are the SAME set by
construction. A second inline copy (a route growing its own `source_finding_id in …` test) is exactly the
drift this guards: it would let the preview and the delete disagree, silently.

Grep-for-a-second-copy, per the ticket. Scoped to the shipped package (`scribble/scribble`), never the
tests, so the assertions here don't count themselves.
"""
from __future__ import annotations

import pathlib

import scribble


def _package_py_files():
    root = pathlib.Path(scribble.__file__).parent
    return list(root.rglob("*.py"))


def test_finding_is_enriched_has_a_single_call_path():
    callers = []
    for py in _package_py_files():
        for lineno, line in enumerate(py.read_text().splitlines(), 1):
            if "finding_is_enriched(" in line and "def finding_is_enriched" not in line:
                callers.append(f"{py.name}:{lineno}: {line.strip()}")
    assert len(callers) == 1, f"expected ONE caller of finding_is_enriched, got: {callers}"
    assert callers[0].startswith("promote.py:"), callers


def test_source_finding_id_membership_is_not_reimplemented_inline():
    """The enriched-membership expression (`source_finding_id in`) must live ONLY inside the predicate —
    a route or template helper that re-derives it inline is a second home for the same decision."""
    hits = []
    for py in _package_py_files():
        for lineno, line in enumerate(py.read_text().splitlines(), 1):
            if "source_finding_id in " in line:
                hits.append(f"{py.name}:{lineno}: {line.strip()}")
    assert len(hits) == 1, f"the enriched-membership test must appear once (in finding_is_enriched): {hits}"
    assert hits[0].startswith("promote.py:"), hits
