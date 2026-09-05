"""#627 — machine-readable JSON + CSV export of an engagement report.

Both formats serialize the SAME ``ReportContext`` the HTML/docx renderers consume (no parallel data
path), so a JSON/CSV export can never disagree with the deliverable. Pins:

- **enum** — ``ReportFormat`` carries ``json`` + ``csv`` alongside ``html``/``docx``;
- **JSON** — a small engagement serializes to valid JSON: engagement metadata, the severity rollup, and
  every report-included finding, each carrying its #626 evidence SHA-256;
- **CSV** — the same engagement serializes to a valid CSV with a header and one row per finding, the
  scalar DTO fields present and the evidence hash carried through;
- **nesting** — a promoted per-host child renders nested in JSON and as its own ``parent_id``-tagged CSV
  row, exactly as the report shows it;
- **route** — the PAT machine ``/report`` route streams both (``?format=json`` / ``?format=csv``), while
  an unknown format is still refused 400.
"""

from __future__ import annotations

import csv
import io
import json

from scribble.enums import ArtifactKind, ArtifactPlacement, ReportFormat, Severity
from scribble.models import Artifact, Engagement, EngagementFinding, FindingGroup
from scribble.reporting import build_report_context
from scribble.reporting.render_csv import render_report_csv
from scribble.reporting.render_json import render_report_json
from tests.conftest import StubActor

# sha256("") — a recognisable digest, so an assertion checks the value, not a shape (matches #626's test).
KNOWN_SHA = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"

M = "/scribble/machine"


def _engagement(session_factory) -> int:
    with session_factory() as db:
        eng = Engagement(name="Export Eng", company_name="Acme Corp", scope_type="external")
        group = FindingGroup(engagement=eng, name="Web App", order_index=0)
        EngagementFinding(
            engagement=eng, group=group, title="Weak SMB signing", severity=Severity.high,
            order_index=0, content_json={}, target_host="10.0.0.5", cvss_score=7.5,
        )
        db.add(eng)
        db.flush()
        db.add(Artifact(
            engagement=eng, finding_id=None, kind=ArtifactKind.screenshot,
            placement=ArtifactPlacement.attached, filename="proof.png", content_type="image/png",
            storage_path="proof.png", caption="Proof", order_index=0, sha256=KNOWN_SHA,
        ))
        db.commit()
        return eng.id


def test_report_format_enum_has_json_and_csv():
    assert ReportFormat.json == "json"
    assert ReportFormat.csv == "csv"


def test_json_export_is_valid_and_carries_the_finding(session_factory):
    eid = _engagement(session_factory)
    with session_factory() as db:
        doc = json.loads(render_report_json(build_report_context(db.get(Engagement, eid))))
    assert doc["engagement"]["name"] == "Export Eng"
    assert doc["rollup"]["overall"] == "high"
    titles = [f["title"] for f in doc["findings"]]
    assert "Weak SMB signing" in titles
    finding = next(f for f in doc["findings"] if f["title"] == "Weak SMB signing")
    assert finding["severity"] == "high"
    assert finding["target_host"] == "10.0.0.5"
    # #626 evidence-integrity hash reaches the export (engagement-level appendix here).
    assert doc["evidence"][0]["sha256"] == KNOWN_SHA


def test_csv_export_is_valid_and_carries_the_finding(session_factory):
    eid = _engagement(session_factory)
    with session_factory() as db:
        text = render_report_csv(build_report_context(db.get(Engagement, eid)))
    rows = list(csv.DictReader(io.StringIO(text)))
    assert len(rows) == 1
    row = rows[0]
    assert row["title"] == "Weak SMB signing"
    assert row["severity"] == "high"
    assert row["group"] == "Web App"
    assert row["target_host"] == "10.0.0.5"
    assert row["cvss_score"] == "7.5"


def test_child_finding_nests_in_json_and_flattens_in_csv(session_factory):
    with session_factory() as db:
        eng = Engagement(name="Nested Eng", company_name="Acme", scope_type="external")
        group = FindingGroup(engagement=eng, name="Hosts", order_index=0)
        parent = EngagementFinding(
            engagement=eng, group=group, title="TLS weakness", severity=Severity.medium,
            order_index=0, content_json={},
        )
        db.add(eng)
        db.flush()  # assigns parent.id (Python-side uuid7 default fires at flush)
        db.add(EngagementFinding(
            engagement=eng, group=group, title="TLS weakness", severity=Severity.medium,
            order_index=1, content_json={}, parent_id=parent.id, target_host="host-b",
        ))
        db.commit()
        ctx = build_report_context(db.get(Engagement, eng.id))

    doc = json.loads(render_report_json(ctx))
    top = doc["findings"]
    assert len(top) == 1  # the child is nested, not top-level
    assert len(top[0]["children"]) == 1
    assert top[0]["children"][0]["target_host"] == "host-b"

    rows = list(csv.DictReader(io.StringIO(render_report_csv(ctx))))
    assert len(rows) == 2  # child is flattened to its own row
    child = next(r for r in rows if r["target_host"] == "host-b")
    assert child["parent_id"] == str(top[0]["id"])


def test_machine_route_streams_json_and_csv(client, stub_host, session_factory):
    with session_factory() as db:
        eng = Engagement(name="Route Eng", scope_type="external", company_name="Acme")
        EngagementFinding(
            engagement=eng, title="Open Redirect", severity=Severity.low, order_index=0,
            content_json={},
        )
        db.add(eng)
        db.commit()
        eid = eng.id

    rj = client.get(f"{M}/engagements/{eid}/report?format=json")
    assert rj.status_code == 200
    assert rj.mimetype == "application/json"
    assert any(f["title"] == "Open Redirect" for f in rj.get_json()["findings"])

    rc = client.get(f"{M}/engagements/{eid}/report?format=csv")
    assert rc.status_code == 200
    assert rc.mimetype == "text/csv"
    rows = list(csv.DictReader(io.StringIO(rc.get_data(as_text=True))))
    assert rows[0]["title"] == "Open Redirect"

    assert client.get(f"{M}/engagements/{eid}/report?format=bogus").status_code == 400


def test_machine_route_json_of_foreign_engagement_is_404(client, stub_host, session_factory):
    with session_factory() as db:
        eng = Engagement(name="Foreign", scope_type="external", company_name="Acme", client_id=999)
        db.add(eng)
        db.commit()
        eid = eng.id
    stub_host.actor = StubActor(id=9, username="stranger", role="operator")
    stub_host.viewable_client_ids = set()
    assert client.get(f"{M}/engagements/{eid}/report?format=json").status_code == 404
