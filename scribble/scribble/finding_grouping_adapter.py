"""Adapt a scribble ``BoardFinding`` into the row dict core's finding-grouping bucketer consumes.

Phase 1b of finding-grouping (core Phase 1 = lotek PR #829). The bucketer itself
(``group_by_kind`` / ``group_by_host``) lives in lotek core (``app.finding_grouping``) and is reached
through the host seam (``scribble.host.group_findings``) — an extension must not ``import app.*``. This
module ONLY maps a finding into the seam's plain row shape; it never groups.

It is a tiny PURE function on purpose: the cross-surface behavioural drift test (core
``tests/test_scribble_ui_mounted.py``) calls it directly, alongside core's own ``reporting.py`` adapter,
and asserts the same input produces identical buckets on both surfaces — the guard the AST single-source
test (``tests/test_finding_grouping_single_source.py``) structurally cannot give (it is name-only,
single-repo). The row keys below MUST equal core's ``GroupItem`` fields.

The two adapters agree by construction because scribble snapshots the source finding at promote
(``dispositions.py``): ``dedupe_key`` and the producing-tool ``source`` both live in
``BoardFinding.source_facts`` with the SAME values core reads from ``Finding.dedupe_key`` /
``Finding.source``, and ``cve_ids`` is already normalized (``metadata.normalize_cve_ids``) exactly as
core's ``normalize_cves`` normalizes ``Finding.cve``.
"""
from __future__ import annotations

from typing import Any


def board_finding_to_group_row(f) -> dict[str, Any]:
    """One ``BoardFinding`` -> the seam row core turns into a ``GroupItem``.

    ``kind_key`` (the by-vulnerability key) mirrors core's ``f.dedupe_key or f"{f.source}:{f.title}"``:
    the promote-snapshotted ``dedupe_key``, else ``"<producing tool>:<title>"`` so no-CVE issues (default
    creds, missing headers) and manually-authored findings still collapse across hosts.
    """
    facts = f.source_facts or {}
    kind_key = facts.get("dedupe_key") or f"{facts.get('source', '')}:{f.title}"
    return {
        "id": str(f.id),
        "host": f.target_host or "",
        "cves": tuple(f.cve_ids or ()),  # already normalized+deduped at promote — do NOT re-normalize
        "severity": f.severity.value,
        "kind_key": kind_key,
        "label": f.title,
        "payload": {"f": f},
    }
