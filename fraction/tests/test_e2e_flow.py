"""WS10 layer A: full-stack integration end-to-end flow (the capstone gate, PLAN.md #13/#9).

Drives ONE engagement entirely through the real HTTP API -- client+engagement creation, group
creation, add-finding-from-template, group reorder, finding move (board drag), artifact upload +
exclude, and finding-meta update (variable resolution + exclude) -- then asserts the resulting board
order is IDENTICAL across all three render paths that consume ``build_report_context``:

    build_report_context(engagement) --> render_report_html(ctx)
                                      `-> render_report_docx(ctx)
                                      `-> export_zip(ctx, ...)

Per docs/RAILS.md #4 ("assert the real end-state, not a proxy" + "fixtures must be able to reveal the
defect"): the Internal group carries FOUR distinct severities (medium, critical, low, high) so an
ordering bug can't hide behind a same-severity fixture, and the arranged manual order
(low, medium, critical) is neither the creation order (medium, critical, low) nor the severity
worst-first order (critical, medium, low) -- in fact it's the exact reverse of severity order -- so a
regression to either wrong order would be caught by every assertion below, not just one.

Fixtures are built from scratch here (own VulnerabilityTemplates, own Client/Engagement) rather than
depending on the FACTION-derived seed text WS12 is actively rewriting.
"""

from __future__ import annotations

import io
import zipfile

import docx
import pytest
from docx.oxml.ns import qn

from fraction.artifacts_storage import resolve_path
from fraction.content import schema
from fraction.enums import Severity
from fraction.models import (
    AssessmentType,
    Engagement,
    EngagementFinding,
    FindingGroup,
    VulnerabilityTemplate,
)
from fraction.reporting import build_report_context
from fraction.reporting.render_docx import render_report_docx
from fraction.reporting.render_html import export_zip, render_report_html

API = "/fraction/api"
UI = "/fraction"

# A tiny valid PNG (1x1) -- real image-header bytes so python-docx's InlineImage will actually embed
# it (same fixture tests/test_report_docx.py uses, for the same reason).
_PNG_BYTES = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108020000009077"
    "53de0000000c4944415478da6360606000000005000166ff0f0e0000000049454e44ae426082"
)

TITLE_MEDIUM = "E2E Medium Finding"
TITLE_CRITICAL = "E2E Critical Finding"
TITLE_LOW = "E2E Low Finding (has TARGET_HOST)"
TITLE_HIGH_EXCLUDED = "E2E High Finding (excluded)"
TITLE_EXTERNAL = "E2E External Finding"

TARGET_HOST_VALUE = "e2e-target.example.test"

# The order the user arranges via two manual drags, after the High finding is excluded.
ARRANGED_INTERNAL_ORDER = [TITLE_LOW, TITLE_MEDIUM, TITLE_CRITICAL]
# What auto_severity (worst-first) would have rendered instead -- the EXACT REVERSE of the arranged
# order, so any regression back toward severity-based sorting is maximally visible, not a coincidence.
SEVERITY_WORST_FIRST_ORDER = [TITLE_CRITICAL, TITLE_MEDIUM, TITLE_LOW]


def _all_docx_text(doc: docx.Document) -> str:
    """Every ``<w:t>`` text run in document order (incl. inside tables) -- mirrors
    ``tests/test_report_docx.py``'s ``_all_text`` helper."""
    return "".join(t.text or "" for t in doc.element.body.iter(qn("w:t")))


def _target_host_block() -> dict:
    return {
        "type": schema.DOC,
        "content": [
            {
                "type": schema.PARAGRAPH,
                "content": [
                    {"type": schema.TEXT, "text": "Affected host: "},
                    {"type": schema.VARIABLE, "attrs": {"key": "TARGET_HOST"}},
                ],
            }
        ],
    }


def _make_template(db, name: str, severity: Severity, *, target_host_block: bool = False) -> int:
    content = (
        {"description": _target_host_block()}
        if target_host_block
        else {"description": schema.doc_from_text(f"{name} description.")}
    )
    tmpl = VulnerabilityTemplate(name=name, category="E2E", default_severity=severity, content_json=content)
    db.add(tmpl)
    db.commit()
    return tmpl.id


@pytest.fixture
def cfg(app):
    return app.extensions["fraction"]


@pytest.fixture
def flow(client, session_factory, cfg):
    """Drive the entire scenario through the real HTTP API and hand back the ids + a reader for the
    artifact bytes it uploaded, so every test below asserts against the SAME arranged state rather
    than re-deriving it (and risking the test and the fixture drifting apart)."""

    # --- 1. client + engagement (POST /engagements/new) -------------------------------------
    resp = client.post(
        f"{UI}/engagements/new",
        data={
            "name": "E2E Flow Engagement",
            "new_client_name": "E2E Flow Client",
            "scope_type": "combined",
            "company_name": "E2E Flow Corp",
        },
    )
    assert resp.status_code == 302

    with session_factory() as db:
        eng = db.query(Engagement).filter_by(name="E2E Flow Engagement").one()
        eng_id = eng.id
        resolved_client = eng.resolve_client(db)
        assert resolved_client is not None
        assert resolved_client.name == "E2E Flow Client"
        at_internal = db.query(AssessmentType).filter_by(slug="internal").first()
        at_external = db.query(AssessmentType).filter_by(slug="external").first()
        at_internal_id = at_internal.id if at_internal else None
        at_external_id = at_external.id if at_external else None

    # --- 2. two FindingGroups, linked to the seeded AssessmentTypes ------------------------
    resp = client.post(
        f"{UI}/engagements/{eng_id}/groups",
        data={"name": "Internal", "assessment_type_id": str(at_internal_id or "")},
    )
    assert resp.status_code == 302
    resp = client.post(
        f"{UI}/engagements/{eng_id}/groups",
        data={"name": "External", "assessment_type_id": str(at_external_id or "")},
    )
    assert resp.status_code == 302

    with session_factory() as db:
        groups = {g.name: g.id for g in db.query(FindingGroup).filter_by(engagement_id=eng_id).all()}
    internal_id, external_id = groups["Internal"], groups["External"]

    # --- 3. four DISTINCT-severity findings from templates into Internal, one into External --
    with session_factory() as db:
        t_med = _make_template(db, TITLE_MEDIUM, Severity.medium)
        t_crit = _make_template(db, TITLE_CRITICAL, Severity.critical)
        t_low = _make_template(db, TITLE_LOW, Severity.low, target_host_block=True)
        t_high = _make_template(db, TITLE_HIGH_EXCLUDED, Severity.high)
        t_ext = _make_template(db, TITLE_EXTERNAL, Severity.info)

    for tmpl_id in (t_med, t_crit, t_low, t_high):
        resp = client.post(
            f"{UI}/engagements/{eng_id}/findings",
            data={"template_id": str(tmpl_id), "group_id": str(internal_id)},
        )
        assert resp.status_code == 302
    resp = client.post(
        f"{UI}/engagements/{eng_id}/findings",
        data={"template_id": str(t_ext), "group_id": str(external_id)},
    )
    assert resp.status_code == 302

    with session_factory() as db:
        by_title = {
            f.title: f.id for f in db.query(EngagementFinding).filter_by(engagement_id=eng_id).all()
        }
    med_id = by_title[TITLE_MEDIUM]
    crit_id = by_title[TITLE_CRITICAL]
    low_id = by_title[TITLE_LOW]
    high_id = by_title[TITLE_HIGH_EXCLUDED]

    # Sanity: before any manual drag, auto_severity renders worst-first (critical, high, medium, low)
    # -- confirms the fixture's starting state is exactly what the rest of this fixture assumes.
    with session_factory() as db:
        ctx0 = build_report_context(db.get(Engagement, eng_id))
    internal0 = next(g for g in ctx0.groups if g.name == "Internal")
    assert [f.title for f in internal0.findings] == [
        TITLE_CRITICAL,
        TITLE_HIGH_EXCLUDED,
        TITLE_MEDIUM,
        TITLE_LOW,
    ]

    # --- 4. drag to a manual order that disagrees with BOTH creation order and severity order -
    # Board (auto_severity, worst-first) currently shows [Critical, High, Medium, Low]. Drag Low to
    # the front -> [Low, Critical, High, Medium] (flips Internal to manual, PLAN.md #4).
    resp = client.post(f"{API}/findings/{low_id}/move", json={"group_id": internal_id, "order_index": 0})
    assert resp.status_code == 200
    assert resp.get_json()["group"]["order_mode"] == "manual"

    # PIN THE FIRST-DRAG RESULT *before* the second drag can launder it. This is the assertion that
    # actually guards the WS3 first-drag bug (docs/RAILS.md #4 incident row 1): dropping Low into slot 0
    # of the displayed board [Critical, High, Medium, Low] must insert it among the neighbours the user
    # SAW -> [Low, Critical, High, Medium]. The pre-fix bug (inserting against raw stored order_index
    # instead of the displayed order) yields [Low, Medium, Critical, High] here. Crucially, the SECOND
    # drag below converges BOTH the correct and buggy states to the identical final order, so asserting
    # only the final order would pass green with the bug present -- this intermediate check is the only
    # thing that can catch it. (High is still included at this point; exclusion happens in step 7.)
    with session_factory() as db:
        ctx_after_first = build_report_context(db.get(Engagement, eng_id))
    internal_after_first = next(g for g in ctx_after_first.groups if g.name == "Internal")
    assert [f.title for f in internal_after_first.findings] == [
        TITLE_LOW,
        TITLE_CRITICAL,
        TITLE_HIGH_EXCLUDED,
        TITLE_MEDIUM,
    ]

    # Now drag Medium up one slot -> [Low, Medium, Critical, High].
    resp = client.post(f"{API}/findings/{med_id}/move", json={"group_id": internal_id, "order_index": 1})
    assert resp.status_code == 200

    # --- 5. reorder the two groups: External before Internal --------------------------------
    resp = client.post(
        f"{API}/engagements/{eng_id}/groups/reorder", json={"order": [external_id, internal_id]}
    )
    assert resp.status_code == 200

    # --- 6. two artifacts on the Low finding; the second toggled excluded --------------------
    resp = client.post(
        f"{API}/artifacts",
        data={
            "engagement_id": str(eng_id),
            "finding_id": str(low_id),
            "caption": "Kept screenshot",
            "file": (io.BytesIO(_PNG_BYTES), "kept.png"),
        },
        content_type="multipart/form-data",
    )
    assert resp.status_code == 201
    kept_artifact_id = resp.get_json()["id"]

    resp = client.post(
        f"{API}/artifacts",
        data={
            "engagement_id": str(eng_id),
            "finding_id": str(low_id),
            "caption": "Excluded screenshot",
            "file": (io.BytesIO(_PNG_BYTES), "excluded.png"),
        },
        content_type="multipart/form-data",
    )
    assert resp.status_code == 201
    excluded_artifact_id = resp.get_json()["id"]

    resp = client.post(f"{API}/artifacts/{excluded_artifact_id}", json={"include_in_report": False})
    assert resp.status_code == 200
    assert resp.get_json()["include_in_report"] is False

    # --- 7. resolve {{TARGET_HOST}} on Low (keep it included); exclude High entirely --------
    resp = client.post(
        f"{UI}/findings/{low_id}",
        data={"target_host": TARGET_HOST_VALUE, "include_in_report": "on"},
    )
    assert resp.status_code == 302
    resp = client.post(f"{UI}/findings/{high_id}", data={})  # include_in_report omitted -> False
    assert resp.status_code == 302

    with session_factory() as db:
        low_finding = db.get(EngagementFinding, low_id)
        assert low_finding.target_host == TARGET_HOST_VALUE
        assert low_finding.include_in_report is True
        assert db.get(EngagementFinding, high_id).include_in_report is False

    def _artifact_bytes(storage_path: str) -> bytes | None:
        try:
            path = resolve_path(cfg, storage_path)
        except ValueError:
            return None
        return path.read_bytes() if path.is_file() else None

    return {
        "eng_id": eng_id,
        "internal_id": internal_id,
        "external_id": external_id,
        "low_id": low_id,
        "med_id": med_id,
        "crit_id": crit_id,
        "high_id": high_id,
        "kept_artifact_id": kept_artifact_id,
        "excluded_artifact_id": excluded_artifact_id,
        "artifact_bytes": _artifact_bytes,
    }


# ---------------------------------------------------------------------------------- 1. context order


def test_context_order_matches_arranged_board_order(session_factory, flow):
    with session_factory() as db:
        engagement = db.get(Engagement, flow["eng_id"])
        ctx = build_report_context(engagement)

    # Group order: the reorder call put External before Internal -- board order == document order.
    assert [g.name for g in ctx.groups] == ["External", "Internal"]

    internal_ctx = next(g for g in ctx.groups if g.name == "Internal")
    assert [f.title for f in internal_ctx.findings] == ARRANGED_INTERNAL_ORDER
    # Prove the fixture isn't degenerate (RAILS.md #4): the arranged order must actually differ from
    # what auto_severity would have produced, or this test could pass even with a pre-fix ordering bug.
    assert [f.title for f in internal_ctx.findings] != SEVERITY_WORST_FIRST_ORDER

    # The excluded finding never reaches the context.
    all_titles = [f.title for g in ctx.groups for f in g.findings]
    assert TITLE_HIGH_EXCLUDED not in all_titles

    # Group metadata (assessment-type link) survived the round trip.
    assert internal_ctx.type_slug == "internal"

    # Artifact include/exclude: only the kept one reaches the Low finding's evidence gallery.
    low_ctx = next(f for f in internal_ctx.findings if f.title == TITLE_LOW)
    assert [a.filename for a in low_ctx.artifacts] == ["kept.png"]


# ------------------------------------------------------------------------------------- 2. HTML report


def test_html_report_matches_context_order_and_resolves_variables(session_factory, flow):
    with session_factory() as db:
        engagement = db.get(Engagement, flow["eng_id"])
        ctx = build_report_context(engagement)
        html_doc = render_report_html(ctx, inline_assets=True, artifact_bytes=flow["artifact_bytes"])

    idx_external = html_doc.index("External")
    idx_internal = html_doc.index("Internal")
    assert idx_external < idx_internal

    # ``.rindex`` (LAST occurrence), not ``.index`` -- the executive-summary narrative (D2) also
    # mentions the worst (critical/high) findings' titles by name, earlier in the document than the
    # finding cards themselves, so a top-severity title's FIRST occurrence can land in the summary
    # rather than its card. The last occurrence is always inside the finding's own rendered card
    # (title in the card header immediately followed by its content block, which for this fixture's
    # templates also echoes the title in "<title> description."), which is reliably in card order
    # since the summary always precedes the groups section.
    idx_low = html_doc.rindex(TITLE_LOW)
    idx_med = html_doc.rindex(TITLE_MEDIUM)
    idx_crit = html_doc.rindex(TITLE_CRITICAL)
    assert idx_low < idx_med < idx_crit

    assert TITLE_HIGH_EXCLUDED not in html_doc

    # {{TARGET_HOST}} resolved INSIDE the finding's content block -- assert the block-specific phrase
    # from ``_target_host_block`` ("Affected host: " + value), which only appears if the in-block
    # variable node actually resolved. A bare ``TARGET_HOST_VALUE in html_doc`` would be an accidental
    # pass: the value also renders as the finding's "Host: " meta chip independent of variable
    # resolution, and the ``"{{" not in`` guard never fires for a builtin key (build_context always
    # defines TARGET_HOST). This distinguishes real resolution from either of those.
    assert "Affected host: " + TARGET_HOST_VALUE in html_doc
    assert "{{" not in html_doc
    assert "}}" not in html_doc

    # The kept artifact is embedded as a real data: URI; the excluded artifact never appears at all.
    assert "Kept screenshot" in html_doc
    assert "data:image/png;base64," in html_doc
    assert "Excluded screenshot" not in html_doc
    assert "excluded.png" not in html_doc


# ------------------------------------------------------------------------------------- 3. DOCX report


def test_docx_report_matches_context_order_and_resolves_variables(session_factory, flow):
    with session_factory() as db:
        engagement = db.get(Engagement, flow["eng_id"])
        ctx = build_report_context(engagement)
        payload = render_report_docx(ctx, artifact_bytes=flow["artifact_bytes"])

    doc = docx.Document(io.BytesIO(payload))
    text = _all_docx_text(doc)

    idx_external = text.index("External")
    idx_internal = text.index("Internal")
    assert idx_external < idx_internal

    # ``.rindex`` (LAST occurrence) -- see the matching comment in the HTML test above: the
    # executive-summary narrative (D2) can also name the top finding earlier in the document.
    idx_low = text.rindex(TITLE_LOW)
    idx_med = text.rindex(TITLE_MEDIUM)
    idx_crit = text.rindex(TITLE_CRITICAL)
    assert idx_low < idx_med < idx_crit

    assert TITLE_HIGH_EXCLUDED not in text

    # Block-specific phrase (see the HTML test): proves the in-block {{TARGET_HOST}} actually resolved,
    # not that the value merely appears somewhere as the target line.
    assert "Affected host: " + TARGET_HOST_VALUE in text
    assert "{{" not in text and "}}" not in text
    assert "{%" not in text and "%}" not in text

    assert "Kept screenshot" in text
    assert len(doc.inline_shapes) == 1  # only the kept artifact embeds; the excluded one never reaches ctx
    assert "Excluded screenshot" not in text


# --------------------------------------------------------------------------------------- 4. ZIP export


def test_export_zip_bundles_report_and_artifacts(session_factory, flow):
    with session_factory() as db:
        engagement = db.get(Engagement, flow["eng_id"])
        ctx = build_report_context(engagement)
        payload = export_zip(ctx, artifact_bytes=flow["artifact_bytes"])

    zf = zipfile.ZipFile(io.BytesIO(payload))
    names = zf.namelist()
    assert "report.html" in names

    # This is the DELIVERY BUNDLE: the excluded artifact must be absent from EVERY output, so exactly
    # ONE artifacts/ file (the kept one) ships and nothing derived from "excluded.png" leaks in. A
    # regression here would ship excluded/sensitive evidence to the client.
    artifact_entries = [n for n in names if n.startswith("artifacts/")]
    assert len(artifact_entries) == 1, artifact_entries
    assert not any("excluded" in n for n in names), names

    report_html = zf.read("report.html").decode("utf-8")
    # ``.rindex`` (LAST occurrence) -- see the matching comment in the HTML test above: the
    # executive-summary narrative (D2) can also name the top finding earlier in the document.
    idx_low = report_html.rindex(TITLE_LOW)
    idx_med = report_html.rindex(TITLE_MEDIUM)
    idx_crit = report_html.rindex(TITLE_CRITICAL)
    assert idx_low < idx_med < idx_crit
    assert TITLE_HIGH_EXCLUDED not in report_html
    assert "Excluded screenshot" not in report_html


# --------------------------------------------------------------------------- 5. the board page itself


def test_board_html_reflects_the_same_arranged_order(client, flow):
    """Ties the loop back to what an author actually sees: the live board page's rendered HTML must
    show the same order as the three deliverables above, not just the in-memory ``ReportContext``."""
    eng_id = flow["eng_id"]
    external_id = flow["external_id"]
    internal_id = flow["internal_id"]
    low_id = flow["low_id"]
    med_id = flow["med_id"]
    crit_id = flow["crit_id"]

    resp = client.get(f"{UI}/engagements/{eng_id}")
    assert resp.status_code == 200
    body = resp.data.decode()

    assert body.index(f'data-group-id="{external_id}"') < body.index(f'data-group-id="{internal_id}"')
    assert (
        body.index(f'data-finding-id="{low_id}"')
        < body.index(f'data-finding-id="{med_id}"')
        < body.index(f'data-finding-id="{crit_id}"')
    )
