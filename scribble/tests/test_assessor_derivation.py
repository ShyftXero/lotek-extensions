"""Assessor = display name(s) of the SOURCE-JOB owners (not created_by). One owner -> that name; several
(internal + external run by different operators) -> "Assessment Team"; none -> None (row omitted).
Resolved via the host's cosmetic job_owners + display_identity seams — no per-viewer actor."""
from __future__ import annotations

from types import SimpleNamespace as NS

import scribble.reporting.context as ctxmod


def _eng(job_ids):
    return NS(findings=[NS(source_facts={"job_id": j} if j else {}) for j in job_ids])


def test_single_owner_resolves_to_that_name(monkeypatch):
    monkeypatch.setattr(ctxmod.host, "job_owners", lambda ids: {j: "u1" for j in ids})
    monkeypatch.setattr(ctxmod.host, "display_identity", lambda uid: NS(name="Eli McRae"))
    assert ctxmod._derive_assessor(_eng(["j1", "j2"])) == "Eli McRae"


def test_multiple_owners_collapse_to_assessment_team(monkeypatch):
    monkeypatch.setattr(ctxmod.host, "job_owners", lambda ids: {"j1": "u1", "j2": "u2"})
    monkeypatch.setattr(ctxmod.host, "display_identity",
                        lambda uid: NS(name={"u1": "Eli McRae", "u2": "Jane Cool"}[uid]))
    assert ctxmod._derive_assessor(_eng(["j1", "j2"])) == "Assessment Team"


def test_no_source_jobs_is_none(monkeypatch):
    monkeypatch.setattr(ctxmod.host, "job_owners", lambda ids: {})
    monkeypatch.setattr(ctxmod.host, "display_identity", lambda uid: None)
    assert ctxmod._derive_assessor(_eng([None])) is None            # findings but no job_id
    assert ctxmod._derive_assessor(_eng([])) is None                # no findings


def test_unmounted_or_unresolvable_owner_is_none(monkeypatch):
    monkeypatch.setattr(ctxmod.host, "job_owners", lambda ids: {})   # unmounted -> {}
    monkeypatch.setattr(ctxmod.host, "display_identity", lambda uid: None)
    assert ctxmod._derive_assessor(_eng(["j1"])) is None
