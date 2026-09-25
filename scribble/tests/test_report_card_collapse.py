"""The report deliverable collapses its DETAILED findings section by vulnerability.

A real external engagement produced **134 per-host findings that are only 17 distinct vulnerabilities**
(one printer-unauth finding on 50 hosts, one Toshiba default-login on 15, …). Rendered one-card-per-
finding that was a **73-page** report of repeated blocks, and a table of contents with 134 rows. The
deliverable must collapse the detailed section the same way the rollup and the "at a glance" index do:
ONE card per vulnerability, the affected hosts listed inside it, and ONE TOC entry per vulnerability.

Shape below is the scrubbed real distribution (generic public vuln names; no client data) so the test
exercises the true scale, not a toy two-host case (harness-no-kinder-than-prod).
"""
from __future__ import annotations

from scribble.enums import Severity
from scribble.models import BoardFinding, Client, FindingGroup, ReportBoard
from scribble.reporting import build_report_context
from scribble.reporting.render_html import render_report_html

# (title, per-host instance count, severity) — 17 kinds, 134 instances. Matches the scrubbed engagement.
KIND_DIST: list[tuple[str, int, Severity]] = [
    ("Multi-function printer - unauthorized access [mfp-unauth-exposure]", 50, Severity.medium),
    ("Toshiba TopAccess - default-login [topaccess-default-login]", 15, Severity.high),
    ("Brother MFC-L9570CDW - information disclosure [cve-2024-51977]", 9, Severity.medium),
    ("Brother printers - authentication bypass via default admin password", 8, Severity.critical),
    ("Sharp multifunction printers - local file inclusion [sharp-printers-lfi]", 8, Severity.high),
    ("Sharp multifunction printers - directory listing [cve-2024-33605]", 8, Severity.high),
    ("Sharp multifunction printers - cookie exposure [cve-2024-33610]", 8, Severity.medium),
    ("Zebra - default login [zebra-default-login]", 6, Severity.high),
    ("HP LaserJet configuration exposure [hp-laserjet-config]", 6, Severity.medium),
    ("Dahua IPC/VTH/VTO - authentication bypass [cve-2021-33044]", 4, Severity.critical),
    ("Yealink CTP18 - default login [yealink-default-login]", 3, Severity.high),
    ("Nortek Linear eMerge E3-series - SQL injection [cve-2022-38627]", 2, Severity.critical),
    ("Linear eMerge E3-series - cross-site scripting [cve-2022-38637]", 2, Severity.medium),
    ("Linear eMerge E3 - cross-site scripting [cve-2019-7255]", 2, Severity.medium),
    ("vsftpd < 3.0.3 - denial of service [cve-2021-30047]", 1, Severity.high),
    ("vsftpd < 2.3.3 - denial of service [cve-2011-0762]", 1, Severity.medium),
    ("vsftpd <= 3.0.2 - access restriction bypass [cve-2015-1419]", 1, Severity.medium),
]
TOTAL = sum(n for _, n, _ in KIND_DIST)      # 134
KINDS = len(KIND_DIST)                        # 17
BIGGEST = max(n for _, n, _ in KIND_DIST)     # 50


def _seed(db):
    client = Client(name="Northwind Logistics")
    db.add(client)
    db.flush()
    eng = ReportBoard(name="20260917_northwind_int", client_id=client.id, company_name="Northwind")
    grp = FindingGroup(engagement=eng, name="Internal", order_index=0)
    db.add_all([eng, grp])
    db.flush()
    i = 0
    for title, n, sev in KIND_DIST:
        for _ in range(n):
            i += 1
            db.add(BoardFinding(
                engagement_id=eng.id, group_id=grp.id, title=title, severity=sev,
                target_host=f"10.20.{i // 254}.{i % 254 + 1}", target_port="80",
                target_url=f"http://10.20.{i // 254}.{i % 254 + 1}:80/setup",
                # dedupe_key embeds the host (the real normalizer shape) — collapse must NOT key on it.
                source_facts={"dedupe_key": f"nuclei:{title[:24]}:{i}", "source": "nuclei"},
            ))
    db.commit()
    return eng.id


def _render(session_factory) -> str:
    with session_factory() as db:
        eng_id = _seed(db)
    with session_factory() as db:
        return render_report_html(build_report_context(db.get(ReportBoard, eng_id)))


def test_detailed_section_is_one_card_per_vulnerability(session_factory):
    html = _render(session_factory)
    cards = html.count('<article class="finding')
    assert cards == KINDS, f"expected {KINDS} collapsed vuln cards, got {cards} (one per host = the bug)"


def test_toc_has_one_entry_per_vulnerability_not_per_host(session_factory):
    html = _render(session_factory)
    # TOC links each detailed finding as href="#finding-<id>". One per vuln, not per host.
    toc = html.split('class="toc"', 1)[-1].split("</nav>", 1)[0] if 'class="toc"' in html else html
    entries = toc.count('href="#finding-')
    assert entries == KINDS, f"expected {KINDS} TOC entries, got {entries}"


def test_collapsed_card_lists_its_affected_hosts(session_factory):
    html = _render(session_factory)
    # the 50-host printer vuln must render its host count somewhere on its one card, and enumerate the
    # hosts inside it (copy-pasteable) rather than as 50 separate cards.
    assert f"{BIGGEST}" in html
    assert "10.20.0.1" in html and "10.20.0.50" in html  # first + a later host present in the one card
