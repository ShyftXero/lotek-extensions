"""ext#42 (Methodology vanishes, leaving a live nav link to an empty anchor) and
ext#45 (back-nav in the document masthead instead of the toolbar).

Both were client-reported on a real deliverable. The #42 half has two parts and the second matters even
if you disagree with the first: a checklist-less engagement rendered NO methodology content, while the
toolbar kept emitting ``<a href="#sec-methodology">`` into a bare empty ``<div>`` — a live link to
nothing, which reads as a broken report to the client clicking it. So the nav is now derived from the
anchors the blocks actually rendered, and a dangling section link is structurally impossible.
"""

from __future__ import annotations

import re

import pytest

from scribble import checklists as C
from scribble.content import schema
from scribble.enums import Severity
from scribble.models import (
    AssessmentType,
    ChecklistTemplate,
    Client,
    Engagement,
    EngagementFinding,
    FindingGroup,
)
from scribble.reporting import build_report_context
from scribble.reporting.layouts import ReportLayout, list_layouts
from scribble.reporting.render_html import _AssetResolver, _render_document, render_report_html


def _block(text: str) -> dict:
    return schema.doc_from_text(text)


def _engagement(session_factory, *, type_slug: str | None = None) -> int:
    with session_factory() as db:
        client = Client(name="TeamsPlus")
        db.add(client)
        db.flush()
        eng = Engagement(name="Web App Assessment", client_id=client.id, company_name="TeamsPlus")
        at = None
        if type_slug is not None:
            at = db.query(AssessmentType).filter_by(slug=type_slug).one()
        grp = FindingGroup(engagement=eng, name="Web Application", order_index=0, assessment_type=at)
        EngagementFinding(
            engagement=eng, group=grp, title="Reflected XSS", severity=Severity.high, order_index=0,
            content_json={"description": _block("Reflected XSS in the search parameter.")},
        )
        db.add_all([eng, grp])
        db.commit()
        return eng.id


def _render(session_factory, **kw) -> str:
    eid = _engagement(session_factory, **kw)
    with session_factory() as db:
        return render_report_html(build_report_context(db.get(Engagement, eid)))


def _topbar(html: str) -> str:
    m = re.search(r'<div class="topbar no-print">.*?</div></div></div>', html, re.S)
    assert m is not None, "no toolbar in the document"
    return m.group(0)


def _masthead(html: str) -> str:
    m = re.search(r"<header class=\"masthead\">.*?</header>", html, re.S)
    assert m is not None, "no masthead in the document"
    return m.group(0)


# ── ext#42: the section exists, and the link is never dangling ────────────────────────────────────


def test_methodology_renders_with_no_checklist_at_all(session_factory):
    """The reported case: an engagement with no checklist. The section is present, titled, and carries
    the standing methodology description instead of silently disappearing."""
    html = _render(session_factory)
    assert '<section class="sec group" id="sec-methodology">' in html
    assert "Methodology" in html
    # the standing description, and an explicit statement of what it is
    # ("Manual validation" until 2026-08-17: the phase is now "Validation", because a heading asserting
    # hand-validation is the same unrecorded claim its body used to make — see
    # tests/test_report_standing_prose.py)
    assert "Validation" in html
    assert "Controlled exploitation and impact assessment" in html
    assert "No engagement-specific coverage checklist was recorded" in html


def test_the_methodology_anchor_is_a_section_not_an_empty_div(session_factory):
    """Before the fix the anchor was a bare ``<div id="sec-methodology"></div>`` followed by an empty
    string — an anchor with no content and no heading."""
    html = _render(session_factory)
    assert '<div id="sec-methodology"></div>' not in html
    anchor_index = html.index('id="sec-methodology"')
    # the anchor belongs to a <section> that has a real heading right after it
    assert html[:anchor_index].rstrip().endswith('<section class="sec group"')
    assert '<h2 class="sec-h">Methodology ' in html[anchor_index:]


def _region_after(html: str, anchor: str) -> str:
    """The slice of the document a reader LANDS ON when they follow ``#anchor``: from the anchor to the
    next section anchor, or to the footer."""
    start = html.index(f'id="{anchor}"')
    rest = html[start + 1 :]
    ends = [m.start() for m in re.finditer(r'id="sec-[a-z0-9-]+"|class="foot"', rest)]
    return rest[: ends[0]] if ends else rest


@pytest.mark.parametrize("layout", [lay.name for lay in list_layouts()])
def test_every_toolbar_section_link_LANDS_ON_CONTENT(session_factory, layout):
    """A toolbar link must lead somewhere a reader can read, for every shipped Layout.

    NOT the ext#42 invariant, and it used to claim it was: ext#42's symptom was a live link to an anchor
    with NO CONTENT — ``<div id="sec-methodology"></div>`` followed by an empty string — and "an element
    carries that id" was already TRUE of the broken build, so the assertion could not fail in either
    direction (second adversarial review, 2026-08-17). Worse, on the fixed code it is a tautology:
    ``_render_document`` builds ``nav_keys`` as ``f'id="sec-{k}"' in html``, so asserting the id is present
    asserts the implementation's own predicate back at itself.

    What is asserted instead is the property the link is FOR: the region between the anchor and the next
    section has real text in it. That is red against the broken build (the reported symptom is precisely an
    empty region) and it is not derivable from ``nav_keys``. The two guards that pin ext#42 proper are
    ``test_methodology_renders_with_no_checklist_at_all`` and
    ``test_the_methodology_anchor_is_a_section_not_an_empty_div``.

    ``findings`` is anchored by a bare ``<div id="sec-findings"></div>`` on purpose — it is a scroll target
    placed above the groups — so the region, not the anchor's own markup, is what has to be measured."""
    eid = _engagement(session_factory)
    with session_factory() as db:
        html = render_report_html(build_report_context(db.get(Engagement, eid)), layout=layout)
    targets = re.findall(r'<a href="#(sec-[a-z]+)"', _topbar(html))
    assert targets, "the toolbar has no section links at all"
    for target in targets:
        assert f'id="{target}"' in html, f'toolbar links #{target} but no element carries that id'
        text = re.sub(r"<[^>]+>", " ", _region_after(html, target))
        text = re.sub(r"\s+", " ", text).strip()
        assert len(text) > 40, (
            f"toolbar links #{target} but the region it scrolls to carries no content ({text!r}) — that is "
            "ext#42's symptom, a live link into an empty anchor"
        )


def test_a_layout_that_drops_the_methodology_block_emits_no_methodology_link(session_factory):
    """``reporting/layouts.py`` says a Layout may drop whole sections. When it does, the toolbar must
    drop the link with it — the nav is derived from what rendered, not from a fixed list."""
    eid = _engagement(session_factory)
    with session_factory() as db:
        ctx = build_report_context(db.get(Engagement, eid))
    bare = ReportLayout("no-method", "No methodology", ("summary", "findings"))
    html = _render_document(ctx, _AssetResolver("none", None), layout=bare)
    assert 'id="sec-methodology"' not in html
    assert 'href="#sec-methodology"' not in html
    # the blocks it DOES carry still get their links
    assert 'href="#sec-summary"' in html and 'href="#sec-findings"' in html


def test_coverage_checklist_still_owns_the_methodology_section(session_factory):
    """With a coverage checklist opted into the report the section keeps its old title and content, and
    now owns the anchor as well (the checklists used to render in a section with no id)."""
    eid = _engagement(session_factory)
    with session_factory() as db:
        eng = db.get(Engagement, eid)
        C.assign_template(db, eng, db.query(ChecklistTemplate).filter_by(slug="web-app-api").one())
        db.commit()
        html = render_report_html(build_report_context(db.get(Engagement, eid)))
    assert "Methodology and Coverage" in html
    assert 'id="sec-methodology"' in html
    assert 'href="#sec-methodology"' in html
    assert "No engagement-specific coverage checklist was recorded" not in html


def test_framing_covers_only_the_assessment_types_this_report_carries(session_factory):
    """The per-type framing is drawn from the report's own sections (``GroupCtx.type_slug``), so it
    describes THIS engagement rather than listing every assessment type Scribble knows about."""
    html = _render(session_factory, type_slug="web-app")
    assert "Web application" in html
    assert "what a legitimate-looking request can be made to do" in html
    assert "Internal network" not in html
    assert "External perimeter" not in html


def test_no_framing_block_when_a_section_has_no_assessment_type(session_factory):
    html = _render(session_factory)
    assert "Framing by section type" not in html


# ── ext#45: navigation belongs in the toolbar ─────────────────────────────────────────────────────


def test_back_links_render_in_the_toolbar(session_factory):
    eid = _engagement(session_factory)
    with session_factory() as db:
        html = render_report_html(
            build_report_context(db.get(Engagement, eid)),
            engagement_url="/scribble/engagements/1",
            dashboard_url="/scribble/",
        )
    topbar = _topbar(html)
    assert 'class="report-nav no-print"' in topbar
    assert "← Dashboard" in topbar and "← Back to engagement" in topbar


def test_back_links_are_not_in_the_document_masthead(session_factory):
    """The defect: the back-links sat INSIDE ``<header class="masthead">``, above the client eyebrow and
    the report title — app chrome wedged into the document's own title block."""
    eid = _engagement(session_factory)
    with session_factory() as db:
        html = render_report_html(
            build_report_context(db.get(Engagement, eid)),
            engagement_url="/scribble/engagements/1",
            dashboard_url="/scribble/",
        )
    masthead = _masthead(html)
    assert "report-nav" not in masthead
    assert "← Dashboard" not in masthead
    assert "← Back to engagement" not in masthead
    # the masthead now opens on the client eyebrow, which is what a cover page is built from
    assert masthead.index('class="eyebrow"') < masthead.index("<h1>")


def test_no_back_links_when_no_urls_are_supplied(session_factory):
    """Standalone/exported rendering passes neither url; the toolbar must not grow an empty nav."""
    html = _render(session_factory)
    # Assert on MARKUP, not on the words: the stylesheet legitimately names both the class and the links
    # in its comments, so a bare substring check would pass or fail on prose.
    assert 'class="report-nav' not in html
    assert ">← Dashboard</a>" not in html
    assert ">← Back to engagement</a>" not in html
