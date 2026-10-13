"""The report card's Affected Assets are clean services; the PoC lives in Reproduction.

Operator feedback (2026-09-25): the exploit URL (an XSS/LFI/SQLi PoC in ``target_url`` / the ``AFFECTED``
overlay) was leaking into the affected-hosts list, and there was no reproduction area for findings whose
PoC lives only in ``target_url``. So: assets render as ``host:port/proto`` only, and Reproduction is its
own section — the authored ``reproduction`` block when present, otherwise derived from the request URL(s).
"""
from __future__ import annotations

from scribble.content import schema
from scribble.enums import Severity
from scribble.models import BoardFinding, Client, FindingGroup, ReportBoard
from scribble.reporting import build_report_context
from scribble.reporting.render_html import render_report_html

EXPLOIT_URL = ("http://10.0.0.5:80/badging/badge_template_print.php?tpl=aa.xml"
               "&idt=1337%20UNION%20SELECT%20pw%20from%20users")


def _render(session_factory, **finding_kwargs) -> str:
    with session_factory() as db:
        client = Client(name="Acme Co")
        db.add(client)
        db.flush()
        eng = ReportBoard(name="E", client_id=client.id, company_name="Acme")
        grp = FindingGroup(engagement=eng, name="Web", order_index=0)
        BoardFinding(engagement=eng, group=grp, title="Nortek eMerge - SQL injection [cve-2022-38627]",
                     severity=Severity.critical, order_index=0, **finding_kwargs)
        db.add(eng)
        db.commit()
        eid = eng.id
    with session_factory() as db:
        return render_report_html(build_report_context(db.get(ReportBoard, eid)))


def _affected_block(html: str) -> str:
    return html.split('class="block affected-assets"', 1)[1].split("</div></div>", 1)[0]


def test_exploit_url_goes_to_reproduction_not_the_asset_list(session_factory):
    # target_url AND the AFFECTED overlay both carry the exploit URL (the real promote shape).
    html = _render(session_factory, target_host="10.0.0.5", target_url=EXPLOIT_URL,
                   variables={"AFFECTED": EXPLOIT_URL})
    aa = _affected_block(html)
    assert "10.0.0.5:80" in aa, "asset renders as the service host:port"
    assert "idt=" not in aa and "UNION" not in aa, "the exploit payload must NOT be in the asset list"
    # Reproduction is its own section, and it carries the request (derived from target_url).
    assert ">Reproduction<" in html
    assert "badge_template_print.php" in html


def test_authored_reproduction_block_wins_over_derived(session_factory):
    html = _render(
        session_factory, target_host="10.0.0.5", target_url=EXPLOIT_URL,
        content_json={
            "description": schema.doc_from_text("A parameter is injectable."),
            "reproduction": schema.doc_from_text("curl 'http://authored.example/poc'"),
        },
    )
    assert "http://authored.example/poc" in html          # the authored PoC renders
    assert html.count(">Reproduction<") == 1              # exactly one — not authored + a derived dupe
    assert "10.0.0.5:80" in _affected_block(html)         # asset still clean (host:port, no /proto)


def test_bare_service_url_yields_no_reproduction(session_factory):
    # A finding whose target_url is just the service root (no path/query) is "exposed at host:port" — it
    # has no PoC request, so no Reproduction section is invented.
    html = _render(session_factory, target_host="10.0.0.9", target_url="http://10.0.0.9:8080/")
    assert "10.0.0.9:8080" in _affected_block(html)
    assert ">Reproduction<" not in html


def _render_parent(session_factory, hosts, *, parent_vars, parent_content=None):
    """Render a report with ONE parent finding over `hosts` (per-host children)."""
    with session_factory() as db:
        client = Client(name="Acme Co")
        db.add(client)
        db.flush()
        eng = ReportBoard(name="E", client_id=client.id, company_name="Acme")
        grp = FindingGroup(engagement=eng, name="Web", order_index=0)
        parent = BoardFinding(engagement=eng, group=grp, title="Reflected XSS", severity=Severity.high,
                              order_index=0, variables=parent_vars, content_json=parent_content)
        db.add(eng)
        db.add(parent)
        db.flush()
        for i, h in enumerate(hosts):
            db.add(BoardFinding(engagement=eng, group=grp, title="Reflected XSS", severity=Severity.high,
                                order_index=i + 1, target_host=h, target_url=f"https://{h}:443/",
                                parent_id=parent.id, variables={"AFFECTED": h}))
        db.commit()
        eid = eng.id
    with session_factory() as db:
        return render_report_html(build_report_context(db.get(ReportBoard, eid)))


def _parent_with_children(session_factory, hosts) -> int:
    """Build (don't render) ONE parent finding over `hosts` (per-host children); return the engagement id."""
    with session_factory() as db:
        client = Client(name="Acme Co")
        db.add(client)
        db.flush()
        eng = ReportBoard(name="E", client_id=client.id, company_name="Acme")
        grp = FindingGroup(engagement=eng, name="Web", order_index=0)
        parent = BoardFinding(engagement=eng, group=grp, title="Reflected XSS", severity=Severity.high,
                              order_index=0, content_json={"description": schema.doc_from_text("XSS.")})
        db.add(eng)
        db.add(parent)
        db.flush()
        for i, h in enumerate(hosts):
            db.add(BoardFinding(engagement=eng, group=grp, title="Reflected XSS", severity=Severity.high,
                                order_index=i + 1, target_host=h, target_url=f"https://{h}:443/",
                                parent_id=parent.id, variables={"AFFECTED": h}))
        db.commit()
        return eng.id


def test_parent_affected_assets_are_not_double_counted(session_factory):
    """A parent's children ARE the asset set; its AFFECTED overlay is a comma-joined copy of the same
    hosts. Folding both double-counted every host (Blade's 93-for-52) and leaked the '…and N more' cap
    sentinel as a fake asset. Count == distinct children; sentinel gone; host:port (no /proto)."""
    aff = _affected_block(_render_parent(
        session_factory, ["10.0.0.1", "10.0.0.2", "10.0.0.3"],
        parent_vars={"AFFECTED": "10.0.0.1:443, 10.0.0.2:443, 10.0.0.3:443 …and 9 more"}))
    assert "Affected Assets (3)" in aff, aff
    assert "more" not in aff and "\u2026" not in aff, "cap sentinel must not leak in as an asset"
    assert "10.0.0.1:443" in aff and "/tcp" not in aff, "host:port only, deduped"


def test_parent_inline_affected_resolves_to_a_count_reference(session_factory):
    """{{AFFECTED}} in a parent's write-up renders as a concise count-reference, not the per-host wall."""
    content = {"description": schema.doc_from_text("Exposed at {{AFFECTED}} without authentication.")}
    html = _render_parent(session_factory, ["10.0.0.1", "10.0.0.2", "10.0.0.3"],
                          parent_vars={"AFFECTED": "10.0.0.1, 10.0.0.2, 10.0.0.3"}, parent_content=content)
    assert "Exposed at 3 hosts (see Affected Assets below) without authentication." in html
    assert "Exposed at 10.0.0.1, 10.0.0.2" not in html, "the raw per-host wall must not render in prose"


def test_docx_affected_hosts_render_as_a_bulleted_list(app, session_factory):
    """The DOCX "Affected Hosts" per-host list must render as real ListBullet paragraphs — one bullet per
    host — not collapse into the green <h4> label run as one run-on line. Regression for the bare-<li> bug:
    html_to_richtext (content/render_docx) opens a list paragraph only for render_html's <li><p>..</p></li>
    shape, so _children_html's bare <li>host</li> gave NO paragraph and every host flowed into the heading
    run. _children_html now wraps each item in <p>."""
    import io
    import zipfile

    from scribble.reporting.render_docx import render_report_docx

    eng_id = _parent_with_children(session_factory, ["a.example", "b.example", "c.example"])
    with app.app_context(), session_factory() as db:
        payload = render_report_docx(build_report_context(db.get(ReportBoard, eng_id)))
    xml = zipfile.ZipFile(io.BytesIO(payload)).read("word/document.xml").decode("utf-8", "replace")
    assert "Affected Hosts" in xml
    # the fix: per-host entries are ListBullet paragraphs. Before it, there were none here and the hosts
    # ran into the heading run.
    assert 'w:val="ListBullet"' in xml, "per-host entries must be bulleted paragraphs, not a run-on"
    assert "a.example" in xml and "b.example" in xml and "c.example" in xml


def test_toolbar_server_export_links_only_with_export_url(session_factory):
    """The report toolbar gets server-side ⬇ PDF / ⬇ DOCX download links ONLY on the live preview (when
    the route supplies export_url). The exported deliverable itself passes no export_url, so a downloaded
    HTML file carries no dead app links."""
    with session_factory() as db:
        client = Client(name="Acme Co")
        db.add(client)
        db.flush()
        eng = ReportBoard(name="E", client_id=client.id, company_name="Acme")
        grp = FindingGroup(engagement=eng, name="Web", order_index=0)
        BoardFinding(engagement=eng, group=grp, title="X", severity=Severity.high, order_index=0)
        db.add(eng)
        db.commit()
        eid = eng.id
    with session_factory() as db:
        ctx = build_report_context(db.get(ReportBoard, eid))
        base = "/scribble/engagements/abc/report/export"
        live = render_report_html(ctx, export_url=base)
        assert f"{base}?format=pdf" in live, "live preview offers the server PDF download"
        assert f"{base}?format=docx" in live
        exported = render_report_html(ctx)  # the deliverable passes no export_url
        assert "?format=pdf" not in exported and "?format=docx" not in exported


def test_export_links_have_spinner_fetch_download_js(session_factory):
    """The ⬇ PDF / ⬇ DOCX links opt into a JS handler that fetches as a blob with a spinner, so a slow
    server render shows progress + a clean failure instead of a silent dead link."""
    with session_factory() as db:
        client = Client(name="Acme Co")
        db.add(client)
        db.flush()
        eng = ReportBoard(name="E", client_id=client.id, company_name="Acme")
        grp = FindingGroup(engagement=eng, name="Web", order_index=0)
        BoardFinding(engagement=eng, group=grp, title="X", severity=Severity.high, order_index=0)
        db.add(eng)
        db.commit()
        eid = eng.id
    with session_factory() as db:
        html = render_report_html(build_report_context(db.get(ReportBoard, eid)),
                                  export_url="/scribble/engagements/abc/report/export")
    assert "data-export" in html                                  # links opt into the handler
    assert 'a[data-export]' in html and "createObjectURL" in html  # fetch-blob download handler
    assert "dl-spin" in html and "@keyframes dl-rot" in html        # spinner CSS


def test_client_display_name_prefers_company_name_over_slug():
    """The single display-name predicate: the client's company_name DISPLAY field wins over the slug
    `name`. On prod resolve_client returns the core `clients` row (which has company_name); the getattr
    default keeps it safe against the standalone scribble_clients model, which has only `name`."""
    from scribble.templating.resolver import client_display_name

    class WithDisplay:
        company_name = "UKG"
        name = "BF-UKG"

    class SlugOnly:
        company_name = ""
        name = "BF-UKG"

    assert client_display_name(WithDisplay()) == "UKG"
    assert client_display_name(SlugOnly()) == "BF-UKG"
    assert client_display_name(None) == ""


def test_cover_carries_assessor_from_created_by(session_factory):
    """The cover now surfaces the assessor (was absent entirely); with no display field on the standalone
    client model, company_name falls back to the slug name rather than crashing."""
    with session_factory() as db:
        client = Client(name="BF-UKG")
        db.add(client)
        db.flush()
        eng = ReportBoard(name="UKG External Pentest", client_id=client.id,
                          company_name=None, created_by="Eli McRae")
        db.add(eng)
        db.commit()
        eid = eng.id
    with session_factory() as db:
        ctx = build_report_context(db.get(ReportBoard, eid))
    assert ctx.assessor == "Eli McRae"    # surfaced from created_by (nothing rendered it before)
    assert ctx.company_name == "BF-UKG"   # standalone client: no display field -> slug, no crash


def test_engagement_company_name_override_wins(session_factory):
    with session_factory() as db:
        client = Client(name="BF-UKG")
        db.add(client)
        db.flush()
        eng = ReportBoard(name="E", client_id=client.id, company_name="UKG Software Inc.")
        db.add(eng)
        db.commit()
        eid = eng.id
    with session_factory() as db:
        ctx = build_report_context(db.get(ReportBoard, eid))
    assert ctx.company_name == "UKG Software Inc."  # explicit engagement override still wins
