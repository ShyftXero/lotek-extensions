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
    return html.split('class="block affected-assets"', 1)[1].split("</details></div>", 1)[0]


def test_exploit_url_goes_to_reproduction_not_the_asset_list(session_factory):
    # target_url AND the AFFECTED overlay both carry the exploit URL (the real promote shape).
    html = _render(session_factory, target_host="10.0.0.5", target_url=EXPLOIT_URL,
                   variables={"AFFECTED": EXPLOIT_URL})
    aa = _affected_block(html)
    assert "10.0.0.5:80/tcp" in aa, "asset renders as the service host:port/proto"
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
    assert "10.0.0.5:80/tcp" in _affected_block(html)     # asset still clean


def test_bare_service_url_yields_no_reproduction(session_factory):
    # A finding whose target_url is just the service root (no path/query) is "exposed at host:port" — it
    # has no PoC request, so no Reproduction section is invented.
    html = _render(session_factory, target_host="10.0.0.9", target_url="http://10.0.0.9:8080/")
    assert "10.0.0.9:8080/tcp" in _affected_block(html)
    assert ">Reproduction<" not in html
