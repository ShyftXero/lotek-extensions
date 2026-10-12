"""target-A: a synthesized PARENT finding's Target host/url must agree between the finding FORM (the
`target_host`/`target_url` COLUMNS) and the report prose (`{{TARGET_HOST}}` in `variables`).

The bug: a parent aggregating many hosts showed a blank Target-host form field while `{{TARGET_HOST}}`
rendered the full ~85-host comma list. Decision A: the Affected-hosts table is the source of truth, so a
multi-host parent is blank-by-design on BOTH surfaces; a single-host parent shows the one host on both.
"""
from __future__ import annotations

import uuid

import scribble.models as fm
from scribble import promote
from tests.conftest import FakeFindingDTO


def _promote(session_factory, hosts):
    """Promote one nuclei finding per host (same template via the dalfox source mapping) and return the
    synthesized parent, re-read."""
    with session_factory() as db:
        eng = fm.ReportBoard(name="Targets", scope_type="external")
        db.add(eng)
        db.commit()
        eng_id, core_id = eng.id, eng.core_engagement_id
    findings = [
        FakeFindingDTO(id=uuid.uuid7(), title="XSS", source="dalfox", target_host=h,
                       dedupe_key=f"dalfox:{h}", facts={"host": h})
        for h in hosts
    ]
    with session_factory() as db:
        engagement = db.get(fm.ReportBoard, eng_id)
        promote.promote_job(db, engagement=engagement, findings=findings,
                            actor_username="t", job_engagement_id=core_id)
        db.commit()
    with session_factory() as db:
        return db.query(fm.BoardFinding).filter_by(
            engagement_id=eng_id, parent_id=None, source_finding_id=None
        ).one()


def test_multi_host_parent_is_blank_on_both_surfaces(app, session_factory):
    parent = _promote(session_factory, ["a.example", "b.example", "c.example"])
    # Form field (column) AND prose variable BOTH blank — the Affected-hosts table carries the detail.
    assert parent.target_host is None
    assert parent.variables.get("TARGET_HOST", "") == ""


def test_single_host_parent_shows_the_host_on_both_surfaces(app, session_factory):
    parent = _promote(session_factory, ["only.example"])
    assert parent.target_host == "only.example"
    assert parent.variables.get("TARGET_HOST") == "only.example"
