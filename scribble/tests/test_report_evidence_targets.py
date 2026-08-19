"""ext#40 — evidence reaches the deliverable no matter WHAT it is attached to.

A client-reported defect, reproduced against a four-artifact matrix (the same matrix
``lotek_triage/repro/repro_report.py`` drives). Content type was never the axis; the ATTACHMENT TARGET
was:

| artifact            | attached to                        | before | after |
|---------------------|------------------------------------|--------|-------|
| control-on-parent   | a top-level finding                | ✓      | ✓     |
| notes.md            | the same finding                   | ✓      | ✓     |
| on-child            | a NESTED CHILD finding             | ✗      | ✓     |
| engagement-level    | the engagement (``finding_id``null)| ✗      | ✓     |

The two failures were independent and both silent: a child was only ever rendered through the text-only
"Affected hosts" table, and ``ReportContext`` had no engagement-level artifact list at all, so an upload
with no ``finding_id`` was stored, answered 201 with a URL, and reached no deliverable ever.
"""

from __future__ import annotations

import io
import re
import zipfile

from scribble.content import schema
from scribble.enums import ArtifactKind, ArtifactPlacement, Severity
from scribble.models import Artifact, Client, Engagement, EngagementFinding, FindingGroup
from scribble.reporting import build_report_context, render_html
from scribble.reporting.render_html import export_zip, render_report_html

# Real PNG header bytes so content-type-driven branches ("is_image") behave as they do in production.
PNG = b"\x89PNG\r\n\x1a\nFAKEDATA"

FILES = {
    "control-on-parent.png": PNG,
    "on-child.png": PNG,
    "engagement-level.png": PNG,
    "notes.md": b"# notes\nsome text evidence\n",
}


def _block(text: str) -> dict:
    return schema.doc_from_text(text)


def _artifact(*, filename, engagement, finding=None, content_type="image/png", **kw) -> Artifact:
    return Artifact(
        engagement=engagement,
        finding=finding,
        kind=ArtifactKind.screenshot if content_type.startswith("image/") else ArtifactKind.text,
        placement=ArtifactPlacement.attached,
        filename=filename,
        content_type=content_type,
        storage_path=filename,
        caption=kw.pop("caption", f"caption for {filename}"),
        order_index=0,
        **kw,
    )


def _matrix(session_factory) -> int:
    """One engagement carrying the full four-artifact matrix above."""
    with session_factory() as db:
        client = Client(name="TeamsPlus")
        db.add(client)
        db.flush()
        eng = Engagement(name="Matrix engagement", client_id=client.id, company_name="TeamsPlus")
        grp = FindingGroup(engagement=eng, name="Web Application", order_index=0)
        parent = EngagementFinding(
            engagement=eng, group=grp, title="CONTROL parent finding", severity=Severity.high,
            order_index=0, content_json={"description": _block("Parent with its own screenshot.")},
        )
        aggregated = EngagementFinding(
            engagement=eng, group=grp, title="AGGREGATED parent finding", severity=Severity.critical,
            order_index=1, content_json={"description": _block("Evidence sits on the child.")},
        )
        db.add_all([eng, grp, parent, aggregated])
        db.flush()
        child = EngagementFinding(
            engagement=eng, group=grp, title="CHILD instance", severity=Severity.critical,
            order_index=0, parent_id=aggregated.id, target_host="portal.teamsplus.example",
            content_json={"description": _block("Child instance.")},
        )
        db.add(child)
        db.flush()
        db.add_all([
            _artifact(filename="control-on-parent.png", engagement=eng, finding=parent),
            _artifact(filename="on-child.png", engagement=eng, finding=child),
            _artifact(filename="engagement-level.png", engagement=eng, finding=None),
            _artifact(filename="notes.md", engagement=eng, finding=parent, content_type="text/markdown"),
        ])
        db.commit()
        return eng.id


def _render(session_factory, eng_id: int) -> str:
    with session_factory() as db:
        ctx = build_report_context(db.get(Engagement, eng_id))
    return render_report_html(ctx, inline_assets=True, artifact_bytes=FILES.get)


def _img_alts(html: str) -> list[str]:
    return re.findall(r'<img[^>]*alt="([^"]*)"', html)


# ── the matrix, one assertion per row ──────────────────────────────────────────────────────────────


def test_artifact_on_a_top_level_finding_renders(session_factory):
    """The control row: this one always worked, and must keep working."""
    html = _render(session_factory, _matrix(session_factory))
    assert 'alt="caption for control-on-parent.png"' in html
    assert "data:image/png;base64," in html


def test_text_artifact_on_a_finding_renders(session_factory):
    """The other control row — the one that made this look like an image-specific bug."""
    html = _render(session_factory, _matrix(session_factory))
    assert "notes.md" in html


def test_artifact_on_a_nested_child_finding_renders(session_factory):
    """ext#40(b): a screenshot attached to a promoted per-host CHILD instance reached no page at all —
    ``_render_children`` was a text-only table whose Evidence cell was the facts line. It now carries the
    child's own gallery, inside the row for the host it belongs to."""
    html = _render(session_factory, _matrix(session_factory))
    assert 'alt="caption for on-child.png"' in html, "child-attached evidence is missing from the report"
    # ...and it renders INSIDE the affected-hosts row for its own host, not loose somewhere in the doc.
    children_table = re.search(r'<details class="children">.*?</details>', html, re.S)
    assert children_table is not None
    row = children_table.group(0)
    assert "portal.teamsplus.example" in row
    assert 'alt="caption for on-child.png"' in row


def test_engagement_level_artifact_renders_in_the_evidence_appendix(session_factory):
    """ext#40(a): an upload with no ``finding_id`` was stored and answered 201 with a URL, but
    ``ReportContext`` exposed no engagement-level artifacts, so it could never appear in a deliverable."""
    html = _render(session_factory, _matrix(session_factory))
    assert 'id="sec-evidence"' in html, "no engagement-level evidence section rendered"
    assert 'alt="caption for engagement-level.png"' in html
    assert 'href="#sec-evidence"' in html, "the Evidence section has no toolbar link"


def test_every_matrix_row_is_present_exactly_where_expected(session_factory):
    """The whole matrix in one place: all three images reach the document (each twice — gallery thumb +
    lightbox), which is the assertion the repro script prints as its verdict."""
    html = _render(session_factory, _matrix(session_factory))
    alts = _img_alts(html)
    for name in ("control-on-parent.png", "on-child.png", "engagement-level.png"):
        assert alts.count(f"caption for {name}") == 2, f"{name} rendered {alts.count(name)} time(s)"


# ── the appendix is genuinely conditional ─────────────────────────────────────────────────────────


def test_no_engagement_artifacts_means_no_evidence_section_and_no_nav_link(session_factory):
    """The normal case: all evidence hangs off findings, so the appendix (and its nav link) is absent
    rather than an empty section with a live link into it."""
    with session_factory() as db:
        eng = Engagement(name="No loose evidence", company_name="Acme")
        db.add(eng)
        db.commit()
        eid = eng.id
    html = _render(session_factory, eid)
    assert 'id="sec-evidence"' not in html
    assert 'href="#sec-evidence"' not in html


def test_engagement_artifact_excluded_from_the_report_stays_out(session_factory):
    """``include_in_report`` governs engagement-level evidence exactly as it governs a finding's."""
    with session_factory() as db:
        eng = Engagement(name="Hidden evidence", company_name="Acme")
        db.add(eng)
        db.flush()
        db.add(_artifact(filename="engagement-level.png", engagement=eng, include_in_report=False))
        db.commit()
        eid = eng.id
    html = _render(session_factory, eid)
    assert 'id="sec-evidence"' not in html
    assert "engagement-level.png" not in html


def test_context_lists_only_unattached_artifacts(session_factory):
    """``ReportContext.artifacts`` is the ENGAGEMENT-level list: a finding's own evidence stays in that
    finding's gallery and must not be duplicated into the appendix."""
    eid = _matrix(session_factory)
    with session_factory() as db:
        ctx = build_report_context(db.get(Engagement, eid))
    assert [a.filename for a in ctx.artifacts] == ["engagement-level.png"]


# ── the child evidence cell ───────────────────────────────────────────────────────────────────────


def test_child_row_with_no_evidence_shows_a_dash_not_a_blank_cell(session_factory):
    with session_factory() as db:
        eng = Engagement(name="Bare child", company_name="Acme")
        grp = FindingGroup(engagement=eng, name="Internal", order_index=0)
        parent = EngagementFinding(
            engagement=eng, group=grp, title="Parent", severity=Severity.high, order_index=0,
            content_json={"description": _block("x")},
        )
        db.add_all([eng, grp, parent])
        db.flush()
        db.add(EngagementFinding(
            engagement=eng, group=grp, title="Child", severity=Severity.high, order_index=0,
            parent_id=parent.id, target_host="host-a.example", content_json={"description": _block("y")},
        ))
        db.commit()
        eid = eng.id
    html = _render(session_factory, eid)
    assert '<td class="child-evidence"></td>' not in html
    assert '<td class="child-evidence"><span class="muted">—</span></td>' in html


# ── delivery formats other than the inlined single file ───────────────────────────────────────────


def test_export_zip_carries_child_and_engagement_evidence(session_factory):
    """The zip deliverable externalizes assets to ``artifacts/``; every reachable artifact must actually
    be written there, or the zip silently ships a broken <img> for exactly the two rows this issue is
    about."""
    eid = _matrix(session_factory)
    with session_factory() as db:
        ctx = build_report_context(db.get(Engagement, eid))
    payload = export_zip(ctx, artifact_bytes=FILES.get)
    with zipfile.ZipFile(io.BytesIO(payload)) as zf:
        names = zf.namelist()
        report = zf.read("report.html").decode()
    assets = [n for n in names if n.startswith("artifacts/")]
    assert len(assets) == 4, f"expected all four artifacts externalized, got {assets}"
    for name in ("on-child.png", "engagement-level.png"):
        assert name in report  # referenced by the document...
        assert any(name.replace(".png", "") in a for a in assets)  # ...and present as a file


# ── what the deliverable CARRIES, not just what it mentions ────────────────────────────────────────
#
# The mechanism this issue was fixed with — surface engagement-level artifacts in the report — also decided
# what those artifacts' BYTES do, and the first version inlined every one of them as a base64 ``data:`` URI.
# Measured against branch HEAD before this guard existed: three 5 MiB captures attached at engagement level
# rendered a 20.0 MiB document (``origin/main`` produced none, because it never read those bytes at all).
# Two things are wrong with that and only one of them is size:
#
#  * ``report.html`` is what a CLIENT receives. A ``.pcap``, a raw scan dump, vector's ``export.html`` is
#    internal working material, and the document cannot render it anyway — it becomes a download chip whose
#    href happens to contain the whole file.
#  * The upload cap is 25 MiB PER artifact and nothing caps the COUNT, so twenty of them build ~660 MB of
#    base64 in one Python string plus an nh3 pass, on EVERY report read (both report routes pass
#    ``inline_assets=True``).
#
# The rule: only images are ever embedded, within a per-asset and a per-render budget; everything else is
# NAMED in the report and delivered by ``export_zip`` as a real file.

PCAP = b"\xd4\xc3\xb2\xa1" + b"\x00" * 4096


def _engagement_with(session_factory, *artifacts_kwargs) -> int:
    with session_factory() as db:
        eng = Engagement(name="Evidence bytes", company_name="Acme")
        db.add(eng)
        db.flush()
        for kw in artifacts_kwargs:
            db.add(_artifact(engagement=eng, **kw))
        db.commit()
        return eng.id


def test_a_non_image_artifact_is_NAMED_but_its_bytes_stay_out_of_the_document(session_factory):
    """The disclosure half. The report has to say the evidence exists — that is ext#40 — without shipping
    a byte-for-byte copy of an internal file inside the client's HTML."""
    eid = _engagement_with(
        session_factory,
        {"filename": "capture.pcap", "content_type": "application/vnd.tcpdump.pcap",
         "caption": "raw capture", "byte_size": len(PCAP)},
    )
    with session_factory() as db:
        ctx = build_report_context(db.get(Engagement, eid))
    html = render_report_html(ctx, inline_assets=True, artifact_bytes=lambda _p: PCAP)

    assert 'id="sec-evidence"' in html, "the appendix must still list it"
    assert "capture.pcap" in html
    assert "raw capture" in html, "the caption is the only thing that says what the file IS"
    assert "not embedded" in html
    assert "data:application" not in html, "the pcap's bytes are inside the deliverable"
    assert "base64" not in html


def test_the_bytes_of_a_non_image_are_never_even_READ(session_factory):
    """Not merely absent from the output: not fetched. A 25 MiB read per artifact per report render is the
    memory half of the defect, so the guard is on the reader, not on the HTML."""
    eid = _engagement_with(
        session_factory,
        {"filename": "scan.xml", "content_type": "application/xml", "byte_size": 4100},
        {"filename": "shot.png", "content_type": "image/png", "byte_size": len(PNG)},
    )
    read: list[str] = []

    def reader(storage_path: str) -> bytes:
        read.append(storage_path)
        return PNG if storage_path.endswith(".png") else PCAP

    with session_factory() as db:
        ctx = build_report_context(db.get(Engagement, eid))
    html = render_report_html(ctx, inline_assets=True, artifact_bytes=reader)
    assert read == ["shot.png"], f"the renderer read bytes it cannot embed: {read}"
    assert "data:image/png;base64," in html


def test_an_image_over_the_PER_ASSET_budget_is_not_embedded(session_factory, monkeypatch):
    """An image is embeddable in principle and still bounded: the upload cap is 25 MiB, and one screenshot
    that large in a data: URI is 33 MiB of one string."""
    monkeypatch.setattr(render_html, "_MAX_INLINE_ASSET_BYTES", 64)
    eid = _engagement_with(
        session_factory,
        {"filename": "huge.png", "content_type": "image/png", "byte_size": 4096},
        {"filename": "small.png", "content_type": "image/png", "byte_size": len(PNG)},
    )
    with session_factory() as db:
        ctx = build_report_context(db.get(Engagement, eid))
    html = render_report_html(
        ctx, inline_assets=True,
        artifact_bytes=lambda p: PNG if p.startswith("small") else b"\x89PNG" + b"\x00" * 4096,
    )
    assert "huge.png" in html and "not embedded" in html
    assert html.count("data:image/png;base64,") == 2, "the small one still embeds (thumb + lightbox)"


def test_a_LYING_byte_size_does_not_get_an_artifact_past_the_budget(session_factory, monkeypatch):
    """``byte_size`` is what the row recorded; the bytes on disk are the authority. A row claiming 10 bytes
    for a 4 KiB file must not be embedded, or the cheap pre-check becomes the whole check."""
    monkeypatch.setattr(render_html, "_MAX_INLINE_ASSET_BYTES", 64)
    eid = _engagement_with(
        session_factory,
        {"filename": "liar.png", "content_type": "image/png", "byte_size": 10},
    )
    with session_factory() as db:
        ctx = build_report_context(db.get(Engagement, eid))
    html = render_report_html(
        ctx, inline_assets=True, artifact_bytes=lambda _p: b"\x89PNG" + b"\x00" * 4096
    )
    assert "not embedded" in html
    assert "data:image/png;base64," not in html


def test_the_PER_RENDER_budget_bounds_a_document_full_of_legal_images(session_factory, monkeypatch):
    """The bound the per-asset cap cannot supply: twenty artifacts each under it still sum to a document
    that has to be built in memory on every report read. Over the total, the rest degrade to the same
    "not embedded" chip rather than the response growing without limit."""
    monkeypatch.setattr(render_html, "_MAX_INLINE_ASSET_BYTES", 4096)
    monkeypatch.setattr(render_html, "_MAX_INLINE_TOTAL_BYTES", 8192)
    img = b"\x89PNG" + b"\x00" * 4092
    eid = _engagement_with(
        session_factory,
        *[
            {"filename": f"shot{i}.png", "content_type": "image/png", "byte_size": len(img)}
            for i in range(10)
        ],
    )
    with session_factory() as db:
        ctx = build_report_context(db.get(Engagement, eid))
    html = render_report_html(ctx, inline_assets=True, artifact_bytes=lambda _p: img)

    embedded = html.count("data:image/png;base64,") // 2  # each embedded image renders thumb + lightbox
    assert embedded == 2, f"the render embedded {embedded} images, i.e. {embedded * 4096} bytes"
    assert html.count("not embedded") == 8, "the rest must be named, not silently dropped"
    for i in range(10):
        assert f"shot{i}.png" in html, "every artifact is still listed"


def test_export_zip_still_delivers_a_non_image_as_a_REAL_FILE(session_factory):
    """The other delivery path is unchanged and is the answer for a non-image: a zip entry is a file, not
    1.33x of itself inside the document."""
    eid = _engagement_with(
        session_factory,
        {"filename": "capture.pcap", "content_type": "application/vnd.tcpdump.pcap",
         "byte_size": len(PCAP)},
    )
    with session_factory() as db:
        ctx = build_report_context(db.get(Engagement, eid))
    payload = export_zip(ctx, artifact_bytes=lambda _p: PCAP)
    with zipfile.ZipFile(io.BytesIO(payload)) as zf:
        entries = [n for n in zf.namelist() if n.startswith("artifacts/")]
        assert len(entries) == 1
        assert zf.read(entries[0]) == PCAP
        assert "not embedded" not in zf.read("report.html").decode()


def test_the_appendix_lists_at_most_MAX_items_and_SAYS_how_many_it_withheld(session_factory, monkeypatch):
    """Belt-and-braces on the count. Truncating a client deliverable's evidence list silently would be the
    same silent omission ext#40 is, so the section says the true total and how many it is not listing."""
    monkeypatch.setattr(render_html, "_MAX_APPENDIX_ITEMS", 3)
    eid = _engagement_with(
        session_factory,
        *[
            {"filename": f"shot{i}.png", "content_type": "image/png", "byte_size": len(PNG)}
            for i in range(5)
        ],
    )
    with session_factory() as db:
        ctx = build_report_context(db.get(Engagement, eid))
    html = render_report_html(ctx, inline_assets=True, artifact_bytes=lambda _p: PNG)

    assert "5 items" in html, "the heading must report the true total"
    assert html.count('class="evidence-item') == 3
    assert "2 further items are recorded against this engagement and not listed here" in html


def test_the_not_embedded_chip_reports_the_size_it_is_not_carrying(session_factory):
    """So the operator reviewing the deliverable knows what the report is pointing at."""
    eid = _engagement_with(
        session_factory,
        {"filename": "capture.pcap", "content_type": "application/vnd.tcpdump.pcap",
         "byte_size": 3 * 1024 * 1024},
    )
    with session_factory() as db:
        ctx = build_report_context(db.get(Engagement, eid))
    html = render_report_html(ctx, inline_assets=True, artifact_bytes=lambda _p: PCAP)
    assert "not embedded · 3.0 MiB" in html
