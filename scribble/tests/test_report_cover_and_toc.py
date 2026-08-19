"""ext#43 — the printed deliverable was a dashboard dump: no cover page, no contents, a one-sentence
executive summary.

Client-reported on a real deliverable and reproduced by ``lotek_triage/repro/repro_report.py``: the
rendered document contained neither ``class="cover"`` nor ``class="toc"``, and page 1 of a real Chrome PDF
opened straight into the masthead. The executive summary was not empty — it was *generated*: a risk banner,
ONE templated sentence, the severity bar, three metric tiles and a findings index. So the complaint was
about structure, and this module pins the three parts of the structure separately:

* the **cover** — print-only title page, built only from front matter the context already carries, taking
  the masthead's place on paper (the masthead is a ``<header>`` BEFORE ``<main>``, so leaving it visible
  would print it ahead of the cover and page 1 would still not be the title page);
* the **contents** — print-only, DERIVED from the template's blocks and the context, with a completeness
  guard in both directions so it can neither link a section the document lacks nor omit one it has;
* the **front matter** in the summary — an engagement overview built from real fields, the standing scope /
  limitations statement, and definitions for the severity ratings the bar and index use.

The browser/PDF cases are SKIP-CLEAN: no Playwright, no Chromium, or no poppler -> skip, never fail.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from datetime import date
from pathlib import Path

import pytest

from scribble import checklists as C
from scribble.content import schema
from scribble.enums import ArtifactKind, ArtifactPlacement, Severity
from scribble.models import (
    Artifact,
    AssessmentType,
    ChecklistTemplate,
    Client,
    Engagement,
    EngagementFinding,
    FindingGroup,
)
from scribble.reporting import build_report_context
from scribble.reporting.render_html import _AssetResolver, _render_document, render_report_html
from scribble.reporting.templates import ReportTemplate, list_templates

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover - exercised by whichever lane lacks the dep
    sync_playwright = None


def _block(text: str) -> dict:
    return schema.doc_from_text(text)


def _full_engagement(session_factory) -> int:
    """One engagement that exercises every kind of section the contents can list: two groups, a nested
    child finding, a coverage checklist (the Methodology section) AND a compliance checklist (its own
    Compliance Attestation section), plus an engagement-level artifact (the Evidence appendix)."""
    with session_factory() as db:
        client = Client(name="TeamsPlus")
        db.add(client)
        db.flush()
        eng = Engagement(
            name="Web Portal Assessment",
            client_id=client.id,
            company_name="TeamsPlus Inc",
            scope_type="web-app",
            start_date=date(2026, 8, 3),
            end_date=date(2026, 8, 12),
            created_by="e.mcrae",  # ASSESSOR resolves from created_by (templating/resolver.py)
        )
        web = db.query(AssessmentType).filter_by(slug="web-app").one()
        grp = FindingGroup(engagement=eng, name="Web Application", order_index=0, assessment_type=web)
        grp2 = FindingGroup(engagement=eng, name="Supporting Infrastructure", order_index=1)
        parent = EngagementFinding(
            engagement=eng, group=grp, title="Exposed Admin Console", severity=Severity.critical,
            order_index=0, content_json={"description": _block("Unauthenticated admin console.")},
        )
        EngagementFinding(
            engagement=eng, group=grp, title="Reflected XSS", severity=Severity.high, order_index=1,
            target_host="portal.teamsplus.example",
            content_json={"description": _block("Reflected XSS in the search parameter.")},
        )
        EngagementFinding(
            engagement=eng, group=grp2, title="Weak TLS Configuration", severity=Severity.low,
            order_index=0, content_json={"description": _block("TLS 1.0 still offered.")},
        )
        db.add_all([eng, grp, grp2, parent])
        db.flush()
        db.add(
            EngagementFinding(
                engagement=eng, group=grp, title="CHILD per-host instance", severity=Severity.critical,
                order_index=0, parent_id=parent.id, target_host="admin.teamsplus.example",
                content_json={"description": _block("Child instance.")},
            )
        )
        db.add(
            Artifact(
                engagement=eng, finding=None, kind=ArtifactKind.screenshot,
                placement=ArtifactPlacement.attached, filename="engagement-level.png",
                content_type="image/png", storage_path="engagement-level.png",
                caption="engagement-level evidence", order_index=0,
            )
        )
        C.assign_template(db, eng, db.query(ChecklistTemplate).filter_by(slug="web-app-api").one())
        C.assign_template(
            db, eng, db.query(ChecklistTemplate).filter_by(slug="pci-dss-segmentation").one()
        )
        db.commit()
        return eng.id


def _bare_engagement(session_factory, **kw) -> int:
    """The opposite pole: a name and one finding, no client, no dates, no assessor, no checklist, no
    engagement-level evidence. Everything optional must be ABSENT rather than rendered empty."""
    with session_factory() as db:
        eng = Engagement(name="Nameless Co Assessment", **kw)
        grp = FindingGroup(engagement=eng, name="Findings", order_index=0)
        EngagementFinding(
            engagement=eng, group=grp, title="Missing Security Headers", severity=Severity.low,
            order_index=0, content_json={"description": _block("No CSP.")},
        )
        db.add_all([eng, grp])
        db.commit()
        return eng.id


def _render(session_factory, eng_id: int, **kw) -> str:
    with session_factory() as db:
        ctx = build_report_context(db.get(Engagement, eng_id))
    return render_report_html(ctx, **kw)


def _render_with_template(session_factory, eng_id: int, template: ReportTemplate) -> str:
    with session_factory() as db:
        ctx = build_report_context(db.get(Engagement, eng_id))
    return _render_document(ctx, _AssetResolver("none", None), template=template)


def _cover(html: str) -> str:
    m = re.search(r'<section class="cover".*?</section>', html, re.S)
    assert m is not None, "no cover page in the document"
    return m.group(0)


def _toc(html: str) -> str:
    m = re.search(r'<nav class="toc".*?</nav>', html, re.S)
    assert m is not None, "no table of contents in the document"
    return m.group(0)


def _topbar(html: str) -> str:
    m = re.search(r'<div class="topbar no-print">.*?</div></div></div>', html, re.S)
    assert m is not None, "no toolbar in the document"
    return m.group(0)


def _toc_targets(html: str) -> list[str]:
    return re.findall(r'<li class="toc-l\d"><a href="#([^"]+)"', _toc(html))


# ── the cover page ────────────────────────────────────────────────────────────────────────────────


def test_the_document_carries_a_cover_page_before_everything_else(session_factory):
    """The reported gap: ``class="cover"`` did not exist in the rendered document at all."""
    html = _render(session_factory, _full_engagement(session_factory))
    assert 'class="cover"' in html
    assert html.index('class="cover"') < html.index('id="sec-summary"')
    cover = _cover(html)
    # front matter, all of it from ReportContext fields the masthead already carried
    assert "Web Portal Assessment" in cover
    assert "TeamsPlus" in cover
    assert "web-app assessment" in cover
    assert "2026-08-03 – 2026-08-12" in cover
    assert "e.mcrae" in cover
    assert "Confidential" in cover
    # ...plus the standing handling notice, which is what makes it a deliverable's title page
    assert "Treat it as confidential" in cover


def test_the_cover_omits_facts_the_engagement_does_not_record(session_factory):
    """A cover page reads as a statement of record, so a field nobody filled in is left OUT rather than
    printed as an em-dash (which reads as "recorded as nothing")."""
    cover = _cover(_render(session_factory, _bare_engagement(session_factory)))
    assert "Testing window" not in cover
    assert "Assessor" not in cover
    assert "Client" not in cover
    # the two facts that are always true of a rendered report stay
    assert "Report date" in cover
    assert "Engagement reference" in cover


def test_the_cover_escapes_engagement_and_client_names(session_factory):
    html = _render(
        session_factory,
        _bare_engagement(session_factory, company_name='<script>alert("x")</script>'),
    )
    assert "<script>alert" not in html
    assert "&lt;script&gt;alert" in _cover(html)


def test_a_template_without_a_cover_block_renders_none_and_keeps_its_masthead(session_factory):
    """The cover is a block, so a template can drop it — and then the body must NOT claim to have one
    (``body.has-cover`` is what suppresses the masthead in print; a stale class would leave that PDF with
    no title anywhere)."""
    eid = _full_engagement(session_factory)
    bare = ReportTemplate("no-cover", "No cover", "auto", ("summary", "findings"))
    html = _render_with_template(session_factory, eid, bare)
    assert 'class="cover"' not in html
    assert "<body>" in html
    assert 'class="has-cover"' not in html
    assert '<header class="masthead">' in html


def test_the_cover_and_contents_get_no_toolbar_links(session_factory):
    """Both are ``display: none`` on screen, so a toolbar link would scroll the reader to an invisible
    element — the same class of broken link as ext#42's link into an empty anchor."""
    topbar = _topbar(_render(session_factory, _full_engagement(session_factory)))
    assert 'href="#sec-cover"' not in topbar
    assert 'href="#sec-toc"' not in topbar
    assert 'href="#sec-summary"' in topbar  # the real ones are still there


# ── the table of contents ─────────────────────────────────────────────────────────────────────────


def test_the_contents_list_every_section_and_finding_in_document_order(session_factory):
    html = _render(session_factory, _full_engagement(session_factory))
    toc = _toc(html)
    assert "Contents" in toc
    for label in (
        "Executive Summary",
        "Web Application",
        "Exposed Admin Console",
        "Reflected XSS",
        "Supporting Infrastructure",
        "Weak TLS Configuration",
        "Methodology and Coverage",
        "Compliance Attestation",
        "Evidence",
    ):
        assert label in toc, f"the contents omit {label!r}"
    # document order: sections in template order, findings under their own group
    assert toc.index("Executive Summary") < toc.index("Web Application") < toc.index("Methodology")
    assert toc.index("Exposed Admin Console") < toc.index("Supporting Infrastructure")
    # a finding entry carries its severity, which is the only "at a glance" a printed contents can give
    assert re.search(r'toc-sev sev-critical">Critical<', toc)


@pytest.mark.parametrize("template", [t.name for t in list_templates()])
def test_every_contents_link_targets_an_anchor_in_the_document(session_factory, template):
    """Half one of the two-way guard: the contents may not link a section the document does not have."""
    eid = _full_engagement(session_factory)
    with session_factory() as db:
        html = render_report_html(build_report_context(db.get(Engagement, eid)), template=template)
    targets = _toc_targets(html)
    assert targets, "the contents have no entries at all"
    for target in targets:
        assert f'id="{target}"' in html, f"the contents link #{target} but nothing carries that id"


@pytest.mark.parametrize("template", [t.name for t in list_templates()])
def test_every_anchored_section_in_the_document_appears_in_the_contents(session_factory, template):
    """Half two, and the one that catches DRIFT: every anchored section of the document must be listed.

    ``_toc_entries`` is a declarative map from block key to entries, so a section added later can be
    forgotten there and go missing from the contents silently — the failure mode this whole issue is about.
    This is the guard that makes that loud. ``sec-cover``/``sec-toc`` are excluded: the front matter does
    not list itself.
    """
    eid = _full_engagement(session_factory)
    with session_factory() as db:
        html = render_report_html(build_report_context(db.get(Engagement, eid)), template=template)
    anchored = {
        m.group(1)
        for m in re.finditer(r'<section class="[^"]*" id="([^"]+)"', html)
        if m.group(1) not in ("sec-cover", "sec-toc")
    }
    assert anchored, "no anchored sections found — the regex or the markup changed"
    missing = anchored - set(_toc_targets(html))
    assert not missing, f"these sections are in the document but not in the contents: {sorted(missing)}"


def test_the_contents_follow_the_template_order(session_factory):
    """The contents are derived from the template's block list, so a template that reorders whole sections
    reorders the contents with it — without the TOC knowing anything about that template."""
    eid = _full_engagement(session_factory)
    toc = _toc(_render(session_factory, eid, template="compliance"))
    assert toc.index("Methodology and Coverage") < toc.index("Web Application")


def test_a_template_that_drops_a_block_drops_its_contents_entry(session_factory):
    eid = _full_engagement(session_factory)
    only_findings = ReportTemplate("just-findings", "Findings only", "auto", ("cover", "toc", "findings"))
    toc = _toc(_render_with_template(session_factory, eid, only_findings))
    assert "Executive Summary" not in toc
    assert "Methodology" not in toc
    assert "Evidence" not in toc
    assert "Web Application" in toc  # the block it DOES carry is still listed


def test_the_contents_omit_nested_child_findings(session_factory):
    """Children render inside their parent's "Affected hosts" table, not as their own cards — so listing
    them in the contents would point at an id that does not exist."""
    toc = _toc(_render(session_factory, _full_engagement(session_factory)))
    assert "CHILD per-host instance" not in toc


def test_no_engagement_evidence_means_no_evidence_entry(session_factory):
    """The Evidence appendix renders only when there is unattached evidence; the contents must follow the
    same condition rather than listing an appendix that is not there."""
    toc = _toc(_render(session_factory, _bare_engagement(session_factory)))
    assert "Evidence" not in toc


def test_the_contents_escape_a_finding_title(session_factory):
    with session_factory() as db:
        eng = Engagement(name="Escaping", company_name="Acme")
        grp = FindingGroup(engagement=eng, name="Findings", order_index=0)
        EngagementFinding(
            engagement=eng, group=grp, title='<img src=x onerror="alert(1)">', severity=Severity.low,
            order_index=0, content_json={"description": _block("x")},
        )
        db.add_all([eng, grp])
        db.commit()
        eid = eng.id
    toc = _toc(_render(session_factory, eid))
    assert "&lt;img src=x" in toc
    assert "<img" not in toc


def test_the_synthetic_ungrouped_bucket_is_listed_and_linkable(session_factory):
    """``build_report_context`` appends a synthetic *Ungrouped* group (``id is None``) for findings with no
    group. Its anchor is ``group-ungrouped``, which is the one anchor not derived from a database id."""
    with session_factory() as db:
        eng = Engagement(name="Loose findings", company_name="Acme")
        EngagementFinding(
            engagement=eng, title="Directory Listing Enabled", severity=Severity.low, order_index=0,
            content_json={"description": _block("Listing on /assets.")},
        )
        db.add(eng)
        db.commit()
        eid = eng.id
    html = _render(session_factory, eid)
    assert 'href="#group-ungrouped"' in _toc(html)
    assert 'id="group-ungrouped"' in html
    assert "Directory Listing Enabled" in _toc(html)


def test_an_engagement_with_no_findings_has_no_dangling_contents_entries(session_factory):
    """``_render_groups`` emits an id-less placeholder section when there are no groups; the contents must
    not invent a link for it."""
    with session_factory() as db:
        eng = Engagement(name="Clean Sweep", company_name="Acme")
        db.add(eng)
        db.commit()
        eid = eng.id
    html = _render(session_factory, eid)
    for target in _toc_targets(html):
        assert f'id="{target}"' in html


# ── front matter in the executive summary ─────────────────────────────────────────────────────────


def test_the_summary_leads_with_prose_and_not_the_dashboard(session_factory):
    """The client's "reads like a printed dashboard": the summary opened on the risk banner and a single
    generated sentence. Prose now comes first, with the banner/chart/tiles as supporting detail below."""
    html = _render(session_factory, _full_engagement(session_factory))
    assert 'class="frontmatter"' in html
    assert html.index('class="frontmatter"') < html.index('<div class="risk ')
    assert "Engagement overview" in html
    assert "Scope and limitations" in html
    # the overview's factual clauses come from real fields
    assert "This report covers a web-app assessment of TeamsPlus Inc." in html
    assert "Testing was carried out over 2026-08-03 – 2026-08-12." in html
    assert "The assessment was performed by e.mcrae." in html
    # the generated narrative is KEPT (it is the second half of the overview, not a thing replaced)
    assert 'class="summary-narrative"' in html
    # ...and the standing limitations, which is what the report claims and does not claim
    assert "not proof that none exists" in html


def test_the_overview_omits_clauses_for_data_the_engagement_lacks(session_factory):
    html = _render(session_factory, _bare_engagement(session_factory))
    assert "Testing was carried out over" not in html
    assert "The assessment was performed by" not in html
    # ``scope_type`` defaults to "external" in the model, and the article follows the value: an
    # operator-supplied scope word must not produce "a external assessment" in a client deliverable.
    assert "This report covers an external assessment of the target environment." in html


def test_the_overview_article_follows_the_scope_word(session_factory):
    html = _render(session_factory, _bare_engagement(session_factory, scope_type="physical"))
    assert "This report covers a physical assessment" in html


def test_severity_ratings_are_defined_under_the_bar_that_uses_them(session_factory):
    """The severity legend showed counts and never said what a rating MEANS — half of the "three plain
    columns of numbers" complaint."""
    html = _render(session_factory, _full_engagement(session_factory))
    assert 'class="sev-defs"' in html
    assert html.index('class="sevbar-wrap"') < html.index('class="sev-defs"')
    assert "How these ratings are used" in html
    for phrase in ("Remediate immediately", "Remediate urgently", "normal maintenance cycle"):
        assert phrase in html


def test_a_clean_engagement_defines_no_ratings(session_factory):
    """No findings, no bar, nothing to explain — the same rule ``_sev_bar`` already follows."""
    with session_factory() as db:
        eng = Engagement(name="Clean Sweep", company_name="Acme")
        db.add(eng)
        db.commit()
        eid = eng.id
    html = _render(session_factory, eid)
    assert 'class="sev-defs"' not in html
    assert "risk-clean" in html  # ...and the clean-report path is otherwise untouched


# ── print media: what the client actually receives ────────────────────────────────────────────────


@pytest.fixture(scope="module")
def browser():
    if sync_playwright is None:
        pytest.skip("playwright is not installed; skipping the print-layout checks (skip-clean)")
    with sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception as exc:  # noqa: BLE001 - any launch failure -> skip-clean
            pytest.skip(f"no usable Chromium runtime ({exc}); skipping print-layout checks (skip-clean)")
        try:
            yield b
        finally:
            b.close()


def _page_for(browser, tmp_path: Path, html: str, name: str = "report.html"):
    path = tmp_path / name
    path.write_text(html, encoding="utf-8")
    page = browser.new_page(viewport={"width": 1400, "height": 1000})
    page.goto(path.as_uri(), wait_until="load")
    return page


def _display(page, selector: str) -> str:
    return page.evaluate(
        """(sel) => {
             const el = document.querySelector(sel);
             if (!el) return "MISSING:" + sel;
             return getComputedStyle(el).display;
           }""",
        selector,
    )


def test_the_cover_and_contents_are_print_only(browser, session_factory, tmp_path):
    """The layout decision, pinned: on screen the sticky toolbar's section jumps and the filterable
    "Findings at a glance" index already do this navigation live, so a static duplicate adds nothing —
    and on paper both of those are gone (``.topbar`` is ``no-print``), which is where they earn their
    place. Print-only also means these two blocks changed nothing on screen — the summary's front matter is
    the only deliberate on-screen change in this issue."""
    page = _page_for(browser, tmp_path, _render(session_factory, _full_engagement(session_factory)))
    try:
        page.emulate_media(media="screen")
        assert _display(page, ".cover") == "none"
        assert _display(page, ".toc") == "none"
        page.emulate_media(media="print")
        assert _display(page, ".cover") != "none"
        assert _display(page, ".toc") != "none"
    finally:
        page.close()


def test_the_masthead_gives_way_to_the_cover_on_paper_only(browser, session_factory, tmp_path):
    """``header.masthead`` sits before ``main``, so it would otherwise print AHEAD of the cover and page 1
    would still not be the title page. On screen the masthead is untouched."""
    page = _page_for(browser, tmp_path, _render(session_factory, _full_engagement(session_factory)))
    try:
        page.emulate_media(media="screen")
        assert _display(page, ".masthead") != "none"
        page.emulate_media(media="print")
        assert _display(page, ".masthead") == "none"
    finally:
        page.close()


def test_a_coverless_template_still_prints_its_masthead(browser, session_factory, tmp_path):
    """The other side of ``body.has-cover``: suppressing the masthead unconditionally would leave a
    template that drops the cover block with no title on paper at all."""
    eid = _full_engagement(session_factory)
    bare = ReportTemplate("no-cover", "No cover", "auto", ("summary", "findings"))
    page = _page_for(
        browser, tmp_path, _render_with_template(session_factory, eid, bare), name="no-cover.html"
    )
    try:
        page.emulate_media(media="print")
        assert _display(page, ".masthead") != "none"
    finally:
        page.close()


def _pdf_page_text(pdf: Path, page_no: int) -> str:
    out = subprocess.run(  # noqa: S603 - fixed argv, path from tmp_path
        ["pdftotext", "-f", str(page_no), "-l", str(page_no), str(pdf), "-"],
        check=True,
        capture_output=True,
        text=True,
    )
    return " ".join(out.stdout.split())


def test_the_printed_pdf_opens_on_the_cover_then_the_contents(browser, session_factory, tmp_path):
    """The end-to-end acceptance check, in the client's own configuration: a real Chrome PDF printed with
    the print dialog's default (background graphics OFF).

    Before this change page 1 was the masthead immediately followed by the executive summary
    (``lotek_triage/evidence/pg_nobg-1.png``). Now page 1 is the title page and page 2 is the contents.
    """
    if shutil.which("pdftotext") is None:  # pragma: no cover - environment-dependent
        pytest.skip("poppler's pdftotext is not installed; skipping the PDF page check (skip-clean)")
    page = _page_for(browser, tmp_path, _render(session_factory, _full_engagement(session_factory)))
    try:
        pdf = tmp_path / "report.pdf"
        page.pdf(path=str(pdf), format="Letter", print_background=False)
    finally:
        page.close()

    first = _pdf_page_text(pdf, 1)
    assert "Web Portal Assessment" in first
    # The badge as the reader sees it: ``.cover-badge`` is ``text-transform: uppercase``, and pdftotext
    # reads the glyphs that were actually painted, not the source text — including the letter-spacing,
    # which it extracts as "CO N F I D E N T I A L". Spaces are stripped for that reason; the check is
    # still specific, because the handling notice's own "confidential" is lower-case.
    assert "CONFIDENTIAL" in first.replace(" ", "")
    assert "Treat it as confidential" in first
    # page 1 is the TITLE page: the dashboard the client was shown instead must not be on it
    assert "Findings by severity" not in first
    assert "Executive Summary" not in first

    second = _pdf_page_text(pdf, 2)
    assert "CONTENTS" in second  # ``.toc-cap`` is uppercased by CSS — see the badge note above
    assert "Exposed Admin Console" in second
    assert "Web Application" in second
