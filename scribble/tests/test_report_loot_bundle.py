"""The report ZIP bundles the whole engagement — its report + its evidence + every scan tool's RAW loot
(nmap/winpeas/sslyze/…), streamed straight from the host object store under ``loot/``. Previously the zip
carried only the evidence a finding cited; raw job loot lived in core's object store and never reached a
report. Pins the export_zip mechanism and the route wiring (with the `?loot=0` opt-out).
"""
from __future__ import annotations

import contextlib
import io
import uuid
import zipfile

import scribble.models as fm
from scribble.enums import Severity
from scribble.reporting import build_report_context
from scribble.reporting.render_html import export_zip

UI = "/scribble"


class _Ref:
    def __init__(self, filename, job_id):
        self.id = uuid.uuid7()
        self.filename = filename
        self.job_id = job_id


class _FakeObjects:
    """A minimal HostObjects stand-in: list() returns artifact refs, open() streams their bytes."""

    def __init__(self, blobs):
        self._blobs = blobs  # list[(_Ref, bytes)]

    def list(self, actor, *, job_id=None, engagement_id=None, kind=None):  # noqa: ARG002
        assert kind == "artifact"  # the bundle asks for raw tool output, not report evidence
        return [ref for ref, _ in self._blobs]

    @contextlib.contextmanager
    def open(self, actor, object_id):  # noqa: ARG002
        data = next(b for ref, b in self._blobs if ref.id == object_id)
        yield io.BytesIO(data)


def _engagement(session_factory) -> uuid.UUID:
    with session_factory() as db:
        eng = fm.ReportBoard(name="Loot Eng", client_id=uuid.uuid7())
        grp = fm.FindingGroup(engagement=eng, name="G", order_index=0)
        db.add_all([eng, grp])
        db.flush()
        db.add(fm.BoardFinding(engagement_id=eng.id, group_id=grp.id, order_index=0,
                               title="V", severity=Severity.high, content_json={}))
        db.commit()
        return eng.id


def test_export_zip_writes_loot_entries(app, session_factory):
    eng_id = _engagement(session_factory)
    loot = [("job-1/nmap.txt", b"NMAP OUTPUT"), ("job-1/winpeas.txt", b"PEAS"),
            ("job-2/sslyze.json", b"{}")]
    with app.app_context(), session_factory() as db:
        ctx = build_report_context(db.get(fm.ReportBoard, eng_id))
        blob = export_zip(ctx, None, loot=loot)
    names = zipfile.ZipFile(io.BytesIO(blob)).namelist()
    assert "report.html" in names
    assert "loot/job-1/nmap.txt" in names
    assert "loot/job-1/winpeas.txt" in names
    assert "loot/job-2/sslyze.json" in names
    assert zipfile.ZipFile(io.BytesIO(blob)).read("loot/job-1/nmap.txt") == b"NMAP OUTPUT"


def test_zip_route_bundles_engagement_loot(client, stub_host, session_factory, monkeypatch):
    eng_id = _engagement(session_factory)
    fake = _FakeObjects([
        (_Ref("nmap.txt", "job-1"), b"NMAP"),
        (_Ref("winpeas.txt", "job-1"), b"PEAS"),
    ])
    monkeypatch.setattr("scribble.host.objects", lambda: fake)

    resp = client.get(f"{UI}/engagements/{eng_id}/report/export?format=zip")
    assert resp.status_code == 200
    names = zipfile.ZipFile(io.BytesIO(resp.data)).namelist()
    assert any(n.startswith("loot/") for n in names), names
    assert sum(n.startswith("loot/") for n in names) == 2

    # ?loot=0 -> the lighter report-only archive, no loot.
    resp0 = client.get(f"{UI}/engagements/{eng_id}/report/export?format=zip&loot=0")
    names0 = zipfile.ZipFile(io.BytesIO(resp0.data)).namelist()
    assert not any(n.startswith("loot/") for n in names0)
