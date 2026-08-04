"""CONTRACT-FACTS §7.2 — THE scribble-side enforcement test: a brand-new tool's declared fact reaches
a RENDERED, POPULATED `{{DOMAIN}}`/`{{AFFECTED}}` with ZERO Python changes on the scribble side.

`scribble/facts.py`/`scribble/promote.py` never see a tool/source name — only the host's neutral
`FindingDTO.facts` dict + the DB-declared `TemplateVariable.from_facts` rules (seeded once, generically,
by `scribble/seed/report_variables.json` — CONTRACT-FACTS §4.3). This test hands `promote.promote_job`
a stub DTO whose `source` ("synthprobe") scribble has never heard of, and proves the mapping + the
render both "just work" — proving the directive ("tools are entirely defined in DATA, not hardcoded
edge cases") end to end, not just at the unit level (`tests/test_facts_shapes.py`).
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import scribble.models as fm
import scribble.promote as promote
from scribble.content import schema
from scribble.reporting.context import build_report_context
from scribble.reporting.render_html import render_report_html
from tests.conftest import FakeFindingDTO


def test_new_tool_fact_populates_DOMAIN_and_AFFECTED_with_zero_python(app, session_factory):
    # 1. A VulnerabilityTemplate whose write-up references the two DoD-gating tokens (CONTRACT.md C4).
    with session_factory() as db:
        eng = fm.Engagement(name="Synthprobe Engagement", scope_type="internal")
        tmpl = fm.VulnerabilityTemplate(
            name="Synthetic DC Probe Finding",
            category="identity",
            content_json={"description": schema.doc_from_text("{{DOMAIN}} — {{AFFECTED}}")},
        )
        db.add_all([eng, tmpl])
        db.commit()
        # Map this new, never-before-seen source to the template (an operator-curated DATA lookup,
        # not a Python branch -- see scribble/promote.py::resolve_vuln_template's own grep proof).
        db.add(fm.ScribbleVulnMap(source="synthprobe", template_id=tmpl.id))
        db.commit()
        eng_id, tmpl_id = eng.id, tmpl.id

    # 2. Hand `promote_job` a stub DTO from a source scribble has never heard of -- facts as the
    # declarative host-side engine would have produced them (CONTRACT-FACTS §2/§3).
    dto = FakeFindingDTO(
        id=555,
        title="Synthetic DC probe finding",
        source="synthprobe",
        target_host="10.9.9.9",
        facts={"domain": "corp.example", "accounts": ["alice", "bob"]},
    )
    with session_factory() as db:
        engagement = db.get(fm.Engagement, eng_id)
        result = promote.promote_job(db, engagement=engagement, findings=[dto], actor_username="tester")
        db.commit()
        assert result == {"promoted": 1, "skipped": 0, "parents": 1}

        parent = db.query(fm.EngagementFinding).filter_by(
            engagement_id=eng_id, template_id=tmpl_id, parent_id=None
        ).one()
        # 3. The DB-declared mapping resolved the neutral facts into real report variables -- no code
        # anywhere decided "synthprobe's accounts list feeds AFFECTED".
        assert parent.variables["AFFECTED"] == "alice, bob"
        assert parent.variables["DOMAIN"] == "corp.example"

    # 4. Render it for real, through a FRESH session (`scribble.db`'s sessionmaker is
    # `expire_on_commit=False` -- the `engagement` object above would still show its PRE-promote,
    # empty `.findings` collection; every other test in this suite re-fetches for the same reason).
    # The literal `{{DOMAIN}}`/`{{AFFECTED}}` tokens must be ABSENT and the real values PRESENT.
    with session_factory() as db:
        engagement = db.get(fm.Engagement, eng_id)
        ctx = build_report_context(engagement)
        html_doc = render_report_html(ctx)
    assert "{{DOMAIN}}" not in html_doc
    assert "{{AFFECTED}}" not in html_doc
    assert "corp.example — alice, bob" in html_doc

    # 5. Assert no scribble SOURCE file learned this tool's name -- the clause that actually enforces
    # the directive (excluding this test file itself and any __pycache__ artifact).
    repo_root = Path(__file__).resolve().parent.parent
    hits = subprocess.run(
        ["grep", "-rIn", "synthprobe", str(repo_root / "scribble")],
        capture_output=True, text=True, check=False,
    )
    assert hits.stdout == "", f"scribble/ source learned the tool's name:\n{hits.stdout}"


def test_this_file_is_the_only_place_synthprobe_is_mentioned():
    """Drift guard for the grep proof above: if a future edit adds `scribble/synthprobe.py` or similar
    for real, this test (not just the runtime grep) should fail loudly in CI."""
    source = Path(__file__).read_text()
    # every mention lives inside this very file's own strings/comments -- sanity-count them.
    assert len(re.findall(r"synthprobe", source)) >= 1
