"""`resolve_vuln_template` tag/cwe matching + precedence, and the promote-time grouping it unlocks.

The `ScribbleVulnMap` used to match only `dedupe_prefix`/`title_pattern`/`source`, so nuclei — the
dominant finding producer — had zero coverage and every hit landed flat/ungrouped (a real report: 118
flat nuclei rows). The durable fix is matching on the finding's own tool CLASSIFICATION (`facts['tags']`
/ `facts['cwe']`), carried on the DTO and previously ignored. One `tag` row covers every template that
carries it, so new nuclei templates inherit a mapping for free.

This pins the two things that make that safe: the precedence order (a specific per-signature row still
wins over the broad class row), and that a tagged nuclei finding actually GROUPS under its template at
promote time instead of staying flat.
"""
from __future__ import annotations

import json
import uuid

import scribble.models as fm
from scribble import promote
from tests.conftest import FakeFindingDTO

# Unique tokens so the real seeded taxonomy (sqli/etc.) can't shadow the precedence fixture.
_T = "zzztest"


def _mk_templates(db, names):
    out = {}
    for n in names:
        t = fm.VulnerabilityTemplate(name=n, active=True)
        db.add(t)
        out[n] = t
    db.commit()
    return {n: t.id for n, t in out.items()}


def test_resolve_precedence_dedupe_title_tag_cwe_source(session_factory, tmp_path):
    """Most-specific-first: dedupe_prefix > title_pattern > tag > cwe > source. Each rung wins only once
    the more-specific signals are removed from the finding."""
    names = [f"{_T}-ByDedupe", f"{_T}-ByTitle", f"{_T}-ByTag", f"{_T}-ByCwe", f"{_T}-BySource"]
    with session_factory() as db:
        ids = _mk_templates(db, names)
    spec = [
        {"name": f"{_T}-ByDedupe", "dedupe_prefix": f"{_T}-ded:"},
        {"name": f"{_T}-ByTitle", "source": f"{_T}-src", "title_pattern": f"{_T}-title*"},
        {"name": f"{_T}-ByTag", "tag": f"{_T}-tag"},
        {"name": f"{_T}-ByCwe", "cwe": "CWE-99999"},
        {"name": f"{_T}-BySource", "source": f"{_T}-src"},
    ]
    path = tmp_path / "map.json"
    path.write_text(json.dumps(spec))
    from scribble.seed.loader import seed_vuln_map
    with session_factory() as db:
        seed_vuln_map(db, json_path=path)
        db.commit()

        def resolve(**kw):
            base = dict(source=f"{_T}-src", title=f"{_T}-title here",
                        dedupe_key=f"{_T}-ded:1", tags=[f"{_T}-tag"], cwe="CWE-99999")
            base.update(kw)
            tid = promote.resolve_vuln_template(db, **base)
            return db.get(fm.VulnerabilityTemplate, tid).name if tid else None

        assert resolve() == f"{_T}-ByDedupe"                      # all signals present → dedupe wins
        assert resolve(dedupe_key=None) == f"{_T}-ByTitle"        # drop dedupe → title
        assert resolve(dedupe_key=None, title=None) == f"{_T}-ByTag"       # → tag
        assert resolve(dedupe_key=None, title=None, tags=None) == f"{_T}-ByCwe"   # → cwe
        assert resolve(dedupe_key=None, title=None, tags=None, cwe=None) == f"{_T}-BySource"  # → source
        # Nothing matches → None (so promote bridges the finding verbatim, unchanged behaviour).
        assert resolve(source="other", title=None, dedupe_key=None, tags=None, cwe=None) is None


def test_shipped_sqli_tag_beats_a_specific_dedupe_refinement(session_factory):
    """A specific nuclei dedupe_prefix refinement outranks the broad tag row (both shipped): a
    `weak-cipher-suites` hit resolves to the TLS template, not whatever tag it also carries."""
    with session_factory() as db:
        tls = promote.resolve_vuln_template(
            db, source="nuclei", title="Weak Cipher Suites",
            dedupe_key="nuclei:weak-cipher-suites:1.2.3.4:host:443",
            tags=["ssl", "tls", "misconfig"], cwe=None,
        )
        assert db.get(fm.VulnerabilityTemplate, tls).name == "Weak TLS/SSL Configuration"


def test_promote_groups_tagged_nuclei_findings_under_one_template(app, session_factory):
    """The real outcome: two `sqli`-tagged nuclei findings on different hosts promote into ONE
    'SQL Injection' parent with two host children — not two flat rows."""
    with session_factory() as db:
        eng = fm.ReportBoard(name="Tag grouping", scope_type="external")
        db.add(eng)
        db.commit()
        eng_id, core_id = eng.id, eng.core_engagement_id

    findings = [
        FakeFindingDTO(id=uuid.uuid7(), title="Blind SQLi [cve-2022-1]", source="nuclei",
                       target_host="a.example", dedupe_key="nuclei:cve-2022-1:a.example",
                       facts={"tags": ["sqli", "cve"]}),
        FakeFindingDTO(id=uuid.uuid7(), title="SQLi in id param [generic-sqli]", source="nuclei",
                       target_host="b.example", dedupe_key="nuclei:generic-sqli:b.example",
                       facts={"tags": ["sqli"]}),
    ]
    with session_factory() as db:
        engagement = db.get(fm.ReportBoard, eng_id)
        result = promote.promote_job(
            db, engagement=engagement, findings=findings, actor_username="tester",
            job_engagement_id=core_id,
        )
        db.commit()
        assert {k: result[k] for k in ("promoted", "skipped", "parents")} == {
            "promoted": 2, "skipped": 0, "parents": 1
        }
        parent = db.query(fm.BoardFinding).filter_by(
            engagement_id=eng_id, parent_id=None, source_finding_id=None
        ).one()
        assert db.get(fm.VulnerabilityTemplate, parent.template_id).name == "SQL Injection"
        children = db.query(fm.BoardFinding).filter_by(parent_id=parent.id).all()
        assert {c.target_host for c in children} == {"a.example", "b.example"}
