"""The report exporter seam: one registry, one `(ctx, opts) -> bytes` shape per format, and a pluggable
PDF conversion backend so scribble can swap docx→PDF engines (LibreOffice today, Gotenberg next) without
touching an exporter or a route.

These pin the seam itself; the mounted route behaviour (PAT `?format=` dispatch, cookie export
disposition) is covered by ``test_report_export_json_csv.py`` / ``test_report_disposition_single_source.py``.
"""
from __future__ import annotations

import json
import uuid

import pytest

from scribble.content import schema
from scribble.enums import Severity
from scribble.models import BoardFinding, FindingGroup, ReportBoard
from scribble.reporting import EXPORTERS, ExportOptions, build_report_context, get_exporter
from scribble.reporting import exporters as EX

RENDER_FORMATS = ("html", "docx", "csv", "json", "zip")


@pytest.fixture(autouse=True)
def _restore_pdf_backend():
    """The selected backend + backend registry are process-global; restore them so one test's fake
    backend can't leak into another (or into a later real render)."""
    original = EX.pdf_backend_name()
    saved = dict(EX._PDF_BACKENDS)
    yield
    EX.set_pdf_backend(original)
    EX._PDF_BACKENDS.clear()
    EX._PDF_BACKENDS.update(saved)


def _engagement(session_factory) -> uuid.UUID:
    with session_factory() as db:
        eng = ReportBoard(name="Exporter Eng", company_name="Acme", scope_type="external")
        group = FindingGroup(engagement=eng, name="Net", order_index=0)
        BoardFinding(
            engagement=eng, group=group, title="Weak thing [cve-2021-33044]", severity=Severity.high,
            order_index=0, cvss_score=7.5, target_host="192.0.2.5", target_port="80",
            target_url="http://192.0.2.5:80/", content_json={"description": schema.doc_from_text("Body.")},
        )
        db.add(eng)
        db.commit()
        return eng.id


def test_registry_lists_exactly_the_known_formats():
    # Exact set: a dropped, renamed, or accidentally-added exporter fails here — this is the drift guard,
    # so it asserts membership rather than a subset.
    assert set(EXPORTERS) == {"html", "docx", "csv", "json", "zip", "pdf"}


@pytest.mark.parametrize(
    ("name", "media_type", "extension"),
    [
        ("html", "text/html; charset=utf-8", "html"),
        ("docx", EX.DOCX_MIME, "docx"),
        ("csv", "text/csv", "csv"),
        ("json", "application/json", "json"),
        ("zip", "application/zip", "zip"),
        ("pdf", "application/pdf", "pdf"),
    ],
)
def test_exporter_metadata(name, media_type, extension):
    exp = EXPORTERS[name]
    assert exp.media_type == media_type
    assert exp.extension == extension


def test_get_exporter_is_case_folded_and_unknown_is_none():
    assert get_exporter("HTML") is EXPORTERS["html"]
    assert get_exporter("  Docx ") is EXPORTERS["docx"]
    assert get_exporter("pdfx") is None
    assert get_exporter(None) is None


@pytest.mark.parametrize("name", RENDER_FORMATS)
def test_every_exporter_renders_nonempty_bytes(app, session_factory, name):
    board_id = _engagement(session_factory)
    exp = EXPORTERS[name]
    with app.app_context(), session_factory() as db:
        ctx = build_report_context(
            db.get(ReportBoard, board_id),
            artifact_url=(lambda _id: "") if exp.inline_url else None,
        )
        out = exp.render(ctx, ExportOptions(artifact_bytes=lambda _sp: None))
    assert isinstance(out, bytes) and out, f"{name} produced no bytes"
    if name in ("docx", "zip"):
        assert out[:2] == b"PK", f"{name} is not a zip container"
    if name == "json":
        assert json.loads(out)  # valid JSON, non-empty
    if name == "html":
        assert b"<" in out


def test_pdf_composes_docx_through_the_selected_backend(app, session_factory):
    # The fake backend wraps whatever bytes it's handed; proving the docx (PK magic) is inside proves the
    # pdf exporter rendered a real docx AND routed it through the configured backend — not a shortcut.
    captured: dict[str, bytes] = {}

    def _fake(docx_bytes: bytes) -> bytes:
        captured["docx"] = docx_bytes
        return b"%PDF-fake\n" + docx_bytes

    EX.register_pdf_backend("fake", _fake)
    EX.set_pdf_backend("fake")
    board_id = _engagement(session_factory)
    with app.app_context(), session_factory() as db:
        ctx = build_report_context(db.get(ReportBoard, board_id), artifact_url=None)
        out = EXPORTERS["pdf"].render(ctx, ExportOptions(artifact_bytes=lambda _sp: None))
    assert out.startswith(b"%PDF-fake")
    assert captured["docx"][:2] == b"PK", "pdf did not compose a real docx before converting"


def test_backend_failure_normalises_to_pdfexporterror():
    def _boom(_docx: bytes) -> bytes:
        raise RuntimeError("soffice not found")

    EX.register_pdf_backend("boom", _boom)
    EX.set_pdf_backend("boom")
    with pytest.raises(EX.PdfExportError) as exc:
        EX.convert_docx_to_pdf(b"PKxx")
    assert "boom" in str(exc.value)


def test_missing_backend_raises_pdfexporterror():
    EX.set_pdf_backend("does-not-exist")
    with pytest.raises(EX.PdfExportError):
        EX.convert_docx_to_pdf(b"PKxx")
