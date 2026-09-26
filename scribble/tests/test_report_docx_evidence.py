"""DOCX evidence: a finding's screenshots render — the finding's own AND every affected-host (child)
screenshot — each figure-captioned, host-labelled for per-host ones. Regression: promoting a finding
per host meant child screenshots were NUMBERED (context.number_figures) but rendered NOWHERE in the docx
(the body stopped carrying children and the structured Affected Assets list kept labels only), leaving a
hole in the figure sequence. `evidence` suppression drops the whole gallery.
"""
from __future__ import annotations

import io
import struct
import uuid
import zipfile

import scribble.models as fm
from scribble.enums import ArtifactKind, ArtifactPlacement, Severity
from scribble.reporting import build_report_context
from scribble.reporting.render_docx import render_report_docx


def _png(w: int = 4, h: int = 4) -> bytes:
    import zlib
    raw = b"".join(b"\x00" + b"\x20\x40\x60" * w for _ in range(h))

    def chunk(t: bytes, d: bytes) -> bytes:
        c = t + d
        return struct.pack(">I", len(d)) + c + struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def _tree(session_factory, *, suppressed=()):
    pngs = {"parent.png": _png(4, 4), "child.png": _png(6, 6)}  # distinct bytes → distinct media parts
    shots: dict[str, bytes] = {}
    with session_factory() as db:
        eng = fm.ReportBoard(name="Ev Eng", client_id=uuid.uuid7())
        grp = fm.FindingGroup(engagement=eng, name="G", order_index=0)
        db.add_all([eng, grp])
        db.flush()
        parent = fm.BoardFinding(engagement_id=eng.id, group_id=grp.id, order_index=0, title="Fleet vuln",
                                 severity=Severity.high, content_json={},
                                 suppressed_sections=list(suppressed))
        db.add(parent)
        db.flush()
        child = fm.BoardFinding(engagement_id=eng.id, group_id=grp.id, parent_id=parent.id, order_index=1,
                                title="Fleet vuln", severity=Severity.high, target_host="192.0.2.9",
                                target_port="80", content_json={})
        db.add(child)
        db.flush()
        for f, name in ((parent, "parent.png"), (child, "child.png")):
            sp = f"obj:{uuid.uuid7()}"
            shots[sp] = pngs[name]
            db.add(fm.Artifact(engagement_id=eng.id, finding_id=f.id, kind=ArtifactKind.screenshot,
                               placement=ArtifactPlacement.attached, filename=name,
                               content_type="image/png", storage_path=sp, byte_size=len(pngs[name])))
        db.commit()
        return eng.id, shots


def _render(app, session_factory, eng_id, shots) -> bytes:
    with app.app_context(), session_factory() as db:
        return render_report_docx(build_report_context(db.get(fm.ReportBoard, eng_id)),
                                  artifact_bytes=lambda sp: shots.get(sp))


def test_docx_embeds_both_own_and_per_host_evidence(app, session_factory):
    eng_id, shots = _tree(session_factory)
    zf = zipfile.ZipFile(io.BytesIO(_render(app, session_factory, eng_id, shots)))
    media = [n for n in zf.namelist() if n.startswith("word/media/")]
    assert len(media) >= 2, f"expected the parent AND the child screenshot embedded, got {media}"
    doc_xml = zf.read("word/document.xml").decode("utf-8", "replace")
    assert "192.0.2.9" in doc_xml  # the per-host (child) figure is host-captioned


def test_evidence_suppression_drops_the_whole_gallery(app, session_factory):
    eng_id, shots = _tree(session_factory, suppressed=["evidence"])
    zf = zipfile.ZipFile(io.BytesIO(_render(app, session_factory, eng_id, shots)))
    media = [n for n in zf.namelist() if n.startswith("word/media/")]
    # Every report embeds the cover MARK (one image), which is not evidence — so the floor is 1, not 0.
    # Evidence suppression must drop every EVIDENCE screenshot (children included): only the cover remains.
    assert len(media) == 1, f"evidence suppressed -> only the cover mark, no evidence images, got {media}"
