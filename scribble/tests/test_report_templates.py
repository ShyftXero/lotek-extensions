"""Report layout templates + richer finding cards (report-vision Phase 5 slice).

Covers the template engine (`reporting/templates.py` + `render_html`'s template-driven document):
- the 3 shipped templates render their blocks in the declared order,
- a template's theme stamps `<html data-theme>` (only for light/dark),
- an unknown `?template=` value falls back to `default` (never raises),
- the layout switcher renders every template as an option,
and the always-present per-finding **Affected Assets** + **Recommendations** sections.
"""

from __future__ import annotations

from scribble.content import schema
from scribble.enums import Severity
from scribble.models import Client, Engagement, EngagementFinding, FindingGroup
from scribble.reporting import build_report_context
from scribble.reporting.render_html import render_report_html
from scribble.reporting.templates import get_template, list_templates


def _block(text: str) -> dict:
    return schema.doc_from_text(text)


def _build(session_factory, *, remediation: bool = True) -> int:
    with session_factory() as db:
        client = Client(name="Acme Co")
        db.add(client)
        db.flush()
        eng = Engagement(name="Templated Assessment", client_id=client.id, company_name="Acme Corp")
        group = FindingGroup(engagement=eng, name="Internal", order_index=0)
        content = {"description": _block("SQL injection in the portal.")}
        if remediation:
            content["remediation"] = _block("Use parameterized queries.")
        EngagementFinding(
            engagement=eng,
            group=group,
            title="SQL Injection",
            severity=Severity.high,
            order_index=0,
            cvss_score=8.6,
            target_host="app.acme.test",
            target_port="443",
            content_json=content,
        )
        db.add(eng)
        db.commit()
        return eng.id


def _render(session_factory, *, template=None, remediation=True) -> str:
    eng_id = _build(session_factory, remediation=remediation)
    with session_factory() as db:
        engagement = db.get(Engagement, eng_id)
        ctx = build_report_context(engagement)
        return render_report_html(ctx, template=template)


# --- template engine ---------------------------------------------------------------------------------

def test_default_template_order_and_no_theme_stamp(session_factory):
    html = _render(session_factory)
    # Block ORDER via the anchor ids (not the nav `href="#sec-*"` links, which are fixed order):
    # summary → findings → methodology.
    assert (
        html.index('id="sec-summary"')
        < html.index('id="sec-findings"')
        < html.index('id="sec-methodology"')
    )
    # "auto" theme leaves the <html> tag unstamped (the CSS still mentions [data-theme] selectors).
    assert '<html lang="en">\n<head>' in html


def test_compliance_template_puts_methodology_before_findings(session_factory):
    html = _render(session_factory, template="compliance")
    assert html.index('id="sec-methodology"') < html.index('id="sec-findings"')


def test_dark_template_stamps_data_theme(session_factory):
    html = _render(session_factory, template="dark")
    assert '<html lang="en" data-theme="dark">' in html


def test_unknown_template_falls_back_to_default(session_factory):
    html = _render(session_factory, template="does-not-exist")
    assert '<html lang="en">\n<head>' in html
    assert html.index('id="sec-findings"') < html.index('id="sec-methodology"')


def test_switcher_lists_every_template(session_factory):
    html = _render(session_factory)
    assert 'id="template-select"' in html
    for t in list_templates():
        assert f'value="{t.name}"' in html
        assert t.label in html
    # the active template is preselected
    assert 'value="default" selected' in html


def test_get_template_fallback():
    assert get_template(None).name == "default"
    assert get_template("  DARK ").name == "dark"  # trimmed + case-insensitive
    assert get_template("nope").name == "default"


# --- richer finding cards ----------------------------------------------------------------------------

def test_finding_carries_affected_assets(session_factory):
    html = _render(session_factory)
    assert "Affected Assets" in html
    assert '<ul class="asset-list">' in html
    assert "app.acme.test:443" in html  # host:port aggregated into the asset list


def test_finding_carries_recommendations(session_factory):
    html = _render(session_factory, remediation=True)
    assert "Recommendations" in html
    assert 'class="block recommendations"' in html
    assert "Use parameterized queries." in html


def test_recommendations_empty_state_when_unauthored(session_factory):
    html = _render(session_factory, remediation=False)
    assert "Recommendations" in html
    assert "No recommendation recorded." in html
