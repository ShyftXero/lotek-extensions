"""Task V proof: the rewritten lotek vuln-DB templates (scribble/seed/lotek_vulnerabilities.json)
render real, specific prose end-to-end -- through the REAL promote path (facts -> declared
TemplateVariable rules -> EngagementFinding.variables), not a hand-built shortcut -- for both HTML and
DOCX, with (a) realistic populated facts and (b) a deliberately empty/absent fact set.

Regression context: an earlier draft of this content wrapped {{DOMAIN}}/{{TARGET_HOST}} in <code> tags
directly in narrative prose (as opposed to inside a literal shell-command example). When the
underlying fact was absent this produced a literal, empty ``<code></code>`` pair in the rendered
output -- a real, visible rendering defect, and exactly the class of bug this task was asked to prove
does NOT happen. These tests pin the fix.
"""

from __future__ import annotations

import io
import re

import docx
from docx.oxml.ns import qn

from scribble.models import Engagement, FindingGroup
from scribble.promote import promote_one
from scribble.reporting import build_report_context
from scribble.reporting.render_docx import render_report_docx
from scribble.reporting.render_html import render_report_html
from tests.conftest import FakeFindingDTO

_LITERAL_TOKEN_RE = re.compile(r"\{\{\w+\}\}")
_EMPTY_TAG_RE = re.compile(r"<(\w+)>\s*</\1>")


def _promote(session_factory, dtos):
    with session_factory() as db:
        eng = Engagement(name="Proof Co", company_name="Acme")
        group = FindingGroup(engagement=eng, name="Findings", order_index=0)
        db.add(eng)
        db.flush()
        for i, dto in enumerate(dtos):
            promote_one(db, engagement=eng, group=group, dto=dto, actor_username="proof", order_index=i)
        db.commit()
        return eng.id


def _render(session_factory, eng_id):
    with session_factory() as db:
        engagement = db.get(Engagement, eng_id)
        ctx = build_report_context(engagement)
        html = render_report_html(ctx)
        docx_bytes = render_report_docx(ctx)
    doc = docx.Document(io.BytesIO(docx_bytes))
    docx_text = "\n".join(t.text or "" for t in doc.element.body.iter(qn("w:t")))
    return ctx, html, docx_text


def test_asrep_kerberoast_certipy_secretsdump_render_with_real_facts(session_factory):
    """The four AD-rollup-family templates, promoted with realistic proven facts (CONTRACT-FACTS.md
    §5.1): DOMAIN/AFFECTED resolve to real values, no literal token survives, no empty tag."""
    dtos = [
        FakeFindingDTO(
            id=1, title="AS-REP roastable accounts", source="asreproast",
            dedupe_key="asreproast:dc01", target_host="dc01.cheddarsale.local",
            facts={"host": "dc01.cheddarsale.local", "domain": "cheddarsale.local",
                   "accounts": ["svc_backup", "krbtgt_svc"]},
        ),
        FakeFindingDTO(
            id=2, title="Kerberoastable SPNs", source="kerberoast",
            dedupe_key="kerberoast:dc01", target_host="dc01.cheddarsale.local",
            facts={"host": "dc01.cheddarsale.local", "domain": "cheddarsale.local",
                   "accounts": ["svc_sql"]},
        ),
        FakeFindingDTO(
            id=3, title="ESC1 template", source="certipy",
            dedupe_key="certipy:ca01", target_host="ca01.cheddarsale.local",
            facts={"host": "ca01.cheddarsale.local", "domain": "cheddarsale.local",
                   "objects": ["WebServerESC1"]},
        ),
        FakeFindingDTO(
            id=4, title="DCSync extraction", source="secretsdump",
            dedupe_key="secretsdump:dc01", target_host="dc01.cheddarsale.local",
            facts={"host": "dc01.cheddarsale.local", "domain": "cheddarsale.local",
                   "accounts_extracted": 9000},
        ),
    ]
    eng_id = _promote(session_factory, dtos)
    ctx, html, docx_text = _render(session_factory, eng_id)

    assert not _LITERAL_TOKEN_RE.search(html), _LITERAL_TOKEN_RE.findall(html)
    assert not _LITERAL_TOKEN_RE.search(docx_text), _LITERAL_TOKEN_RE.findall(docx_text)
    assert not _EMPTY_TAG_RE.search(html)

    # A matched ScribbleVulnMap entry promotes with the LIBRARY TEMPLATE's own name (not the raw scan
    # finding's title) -- see EngagementFinding.from_template.
    findings = {f.title: f for f in ctx.groups[0].findings}
    assert findings["AS-REP Roasting"].variables["DOMAIN"] == "cheddarsale.local"
    assert findings["AS-REP Roasting"].variables["AFFECTED"] == "krbtgt_svc, svc_backup"
    assert "krbtgt_svc, svc_backup" in findings["AS-REP Roasting"].blocks_html["details"]
    esc = findings["AD CS - Vulnerable Certificate Template or CA (ESC)"]
    assert esc.variables["AFFECTED"] == "WebServerESC1"
    dcsync = findings["DCSync - Domain Credential Database Extraction"]
    assert dcsync.variables["AFFECTED"] == "9000 domain accounts"
    assert "9000 domain accounts" in dcsync.blocks_html["details"]


def test_weak_credentials_uses_accounts_not_host_in_affected(session_factory):
    """Regression pin for the content bug this task fixed: the shipped 'Weak or Guessable Credentials'
    template originally read '...accounts on {{AFFECTED}}...' which assumed AFFECTED was host-shaped --
    but brutus's declared facts put ``accounts`` (usernames) FIRST in AFFECTED's cascade, so it actually
    rendered as '...accounts on alice, bob...'. The rewritten template must show the confirmed
    username(s), not silently mis-render a host where an account belongs."""
    dto = FakeFindingDTO(
        id=1, title="Weak credential", source="brutus", target_host="10.0.0.5",
        facts={"accounts": ["admin"]},
    )
    eng_id = _promote(session_factory, [dto])
    ctx, html, docx_text = _render(session_factory, eng_id)

    assert not _LITERAL_TOKEN_RE.search(html)
    finding = ctx.groups[0].findings[0]
    assert finding.variables["AFFECTED"] == "admin"
    assert "admin" in finding.blocks_html["details"]
    # No dangling artifact from the removed inline TARGET_HOST reference.
    assert not _EMPTY_TAG_RE.search(html)


def test_llmnr_poisoning_renders_captured_accounts(session_factory):
    dto = FakeFindingDTO(
        id=1, title="NTLMv2 hash captured", source="responder",
        facts={"domain": "CORP", "accounts": ["alice", "bob"]},
    )
    eng_id = _promote(session_factory, [dto])
    ctx, html, docx_text = _render(session_factory, eng_id)
    finding = ctx.groups[0].findings[0]
    assert finding.variables["AFFECTED"] == "alice, bob"
    assert "Credentials captured: alice, bob" in finding.blocks_html["details"]
    assert not _LITERAL_TOKEN_RE.search(html)


def test_asrep_template_degrades_cleanly_with_no_facts_at_all(session_factory):
    """The adversarial case the task explicitly requires: promote a finding that resolves to a touched
    template but carries NO facts and NO target_host (the worst realistic input) and confirm the
    rendered HTML/DOCX has no literal {{TOKEN}}, and -- the concrete bug this task fixed -- no empty
    ``<code></code>`` tag from a blanked DOMAIN/TARGET_HOST reference in narrative prose."""
    dto = FakeFindingDTO(
        id=1, title="AS-REP roastable accounts (bare)", source="asreproast",
        dedupe_key="asreproast:bare", target_host=None, facts={},
    )
    eng_id = _promote(session_factory, [dto])
    ctx, html, docx_text = _render(session_factory, eng_id)

    assert not _LITERAL_TOKEN_RE.search(html), _LITERAL_TOKEN_RE.findall(html)
    assert not _LITERAL_TOKEN_RE.search(docx_text), _LITERAL_TOKEN_RE.findall(docx_text)
    assert not _EMPTY_TAG_RE.search(html), _EMPTY_TAG_RE.findall(html)

    finding = ctx.groups[0].findings[0]
    assert finding.variables["DOMAIN"] == ""
    assert finding.variables["AFFECTED"] == ""
    # The narrative description itself (which no longer embeds DOMAIN inline) still reads as a full,
    # grammatical sentence with no artifact from the missing fact.
    assert "Do not require Kerberos pre-authentication" in finding.blocks_html["description"]


def test_xss_and_kubernetes_render_with_real_facts(session_factory):
    """Two more templates touched by this task, exercised through their real seeded ScribbleVulnMap
    source match (dalfox / kubescape) rather than the AD/network family."""
    dtos = [
        FakeFindingDTO(
            id=1, title="Reflected XSS in search", source="dalfox",
            facts={"url": "https://shop.test/search?q=1", "param": "q"},
        ),
        FakeFindingDTO(
            id=2, title="Privileged pod", source="kubescape",
            facts={"control_id": "C-0012"},
        ),
    ]
    eng_id = _promote(session_factory, dtos)
    ctx, html, docx_text = _render(session_factory, eng_id)
    assert not _LITERAL_TOKEN_RE.search(html)
    assert not _EMPTY_TAG_RE.search(html)

    findings = {f.title: f for f in ctx.groups[0].findings}
    assert findings["Cross-Site Scripting (XSS)"].variables["AFFECTED"] == "parameter 'q'"
    assert findings["Kubernetes Security Misconfiguration"].variables["AFFECTED"] == "C-0012"
