"""Report Layouts and Themes as two independent axes (#100), plus richer finding cards.

Covers the split (`reporting/layouts.py`, `reporting/themes.py`, `reporting/selection.py` and
`render_html`'s Layout-driven document):

- each shipped Layout renders its blocks in the declared order,
- a Theme stamps `<html data-theme>` (only for light/dark; `auto` stamps nothing),
- **orthogonality**: every Layout renders under every Theme — the property the split exists to buy,
- unknown `?layout=` / `?theme=` values fall back rather than raising,
- the legacy single-axis `?template=` still resolves, including `dark` -> default layout + dark theme,
- an explicit axis beats a stale legacy value,
- both switchers render with the active option preselected,

and the always-present per-finding **Affected Assets** + **Recommendations** sections.
"""

from __future__ import annotations

import itertools

import pytest

from scribble.content import schema
from scribble.enums import Severity
from scribble.models import Client, Engagement, EngagementFinding, FindingGroup
from scribble.reporting import build_report_context
from scribble.reporting.layouts import get_layout, list_layouts
from scribble.reporting.render_html import render_report_html
from scribble.reporting.selection import resolve_selection
from scribble.reporting.themes import get_theme, list_themes


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


def _render(session_factory, *, remediation: bool = True, **kw) -> str:
    eng_id = _build(session_factory, remediation=remediation)
    with session_factory() as db:
        engagement = db.get(Engagement, eng_id)
        ctx = build_report_context(engagement)
        return render_report_html(ctx, **kw)


# --- Layouts -----------------------------------------------------------------------------------------

def test_default_layout_order(session_factory):
    html = _render(session_factory)
    # Block ORDER via the anchor ids (not the nav `href="#sec-*"` links, which are fixed order).
    assert (
        html.index('id="sec-summary"')
        < html.index('id="sec-findings"')
        < html.index('id="sec-methodology"')
    )


def test_compliance_layout_puts_methodology_before_findings(session_factory):
    html = _render(session_factory, layout="compliance")
    assert html.index('id="sec-methodology"') < html.index('id="sec-findings"')


def test_unknown_layout_falls_back_to_default(session_factory):
    html = _render(session_factory, layout="does-not-exist")
    assert html.index('id="sec-findings"') < html.index('id="sec-methodology"')


def test_get_layout_fallback():
    assert get_layout(None).name == "default"
    assert get_layout("  COMPLIANCE ").name == "compliance"  # trimmed + case-insensitive
    assert get_layout("nope").name == "default"


def test_a_layout_carries_no_appearance():
    """The whole point of the split: a Layout has no theme/palette attribute to carry."""
    layout = get_layout("default")
    assert not hasattr(layout, "theme")
    assert not hasattr(layout, "stamp")


# --- Themes ------------------------------------------------------------------------------------------

def test_auto_theme_stamps_nothing(session_factory):
    html = _render(session_factory)
    assert '<html lang="en">\n<head>' in html


def test_dark_theme_stamps_data_theme(session_factory):
    html = _render(session_factory, theme="dark")
    assert '<html lang="en" data-theme="dark">' in html


def test_light_theme_stamps_data_theme(session_factory):
    """`light` was a legal value of the old THEMES tuple that no shipped entry ever used, so a
    guaranteed-light report was unreachable. It is selectable now."""
    html = _render(session_factory, theme="light")
    assert '<html lang="en" data-theme="light">' in html


def test_unknown_theme_falls_back_to_auto(session_factory):
    html = _render(session_factory, theme="chartreuse")
    assert '<html lang="en">\n<head>' in html


def test_get_theme_fallback():
    assert get_theme(None).name == "auto"
    assert get_theme("  DARK ").name == "dark"  # trimmed + case-insensitive
    assert get_theme("nope").name == "auto"


def test_a_theme_carries_no_structure():
    """Converse of the Layout check: a Theme has no blocks to reorder."""
    assert not hasattr(get_theme("dark"), "blocks")


# --- orthogonality: the property the split buys -------------------------------------------------------

@pytest.mark.parametrize(
    ("layout", "theme"),
    list(itertools.product([lay.name for lay in list_layouts()], [t.name for t in list_themes()])),
)
def test_every_layout_renders_under_every_theme(session_factory, layout, theme):
    """Before #100 this matrix was unreachable: appearance was a field on the layout, so a pairing
    existed only if someone had added a registry row for it. `compliance` in dark had no row."""
    html = _render(session_factory, layout=layout, theme=theme)
    expected_stamp = get_theme(theme).html_attr
    assert f'<html lang="en"{expected_stamp}>' in html
    # ...and the Layout still drove the structure, independently of the Theme: whichever of its blocks
    # rendered an anchor did so in the Layout's declared order. (A block may legitimately render
    # nothing — an engagement with no diagrams, say — so absent anchors are skipped, not failed.)
    positions = [
        html.index(f'id="sec-{block}"')
        for block in get_layout(layout).blocks
        if f'id="sec-{block}"' in html
    ]
    assert len(positions) >= 2, "expected at least two anchored sections to compare order"
    assert positions == sorted(positions)


def test_compliance_in_dark_is_reachable(session_factory):
    """The concrete pairing that had no registry row before the split."""
    html = _render(session_factory, layout="compliance", theme="dark")
    assert '<html lang="en" data-theme="dark">' in html
    assert html.index('id="sec-methodology"') < html.index('id="sec-findings"')


# --- legacy ?template= -------------------------------------------------------------------------------

def test_legacy_template_dark_maps_to_default_layout_plus_dark_theme(session_factory):
    """`dark` was never a distinct structure — only the standard blocks with a forced palette. A
    bookmarked ?template=dark URL must keep producing a dark report."""
    html = _render(session_factory, template="dark")
    assert '<html lang="en" data-theme="dark">' in html
    assert html.index('id="sec-findings"') < html.index('id="sec-methodology"')


def test_legacy_template_compliance_maps_to_compliance_layout(session_factory):
    html = _render(session_factory, template="compliance")
    assert html.index('id="sec-methodology"') < html.index('id="sec-findings"')
    assert '<html lang="en">\n<head>' in html  # auto theme, unstamped


def test_unknown_legacy_template_falls_back(session_factory):
    html = _render(session_factory, template="does-not-exist")
    assert '<html lang="en">\n<head>' in html
    assert html.index('id="sec-findings"') < html.index('id="sec-methodology"')


def test_explicit_axis_beats_a_stale_legacy_template():
    """The switcher writes ?layout=/?theme= and deletes ?template=, but a hand-edited URL can carry
    both. An explicit axis wins on its own axis; the legacy value fills only what is unspecified."""
    layout, theme = resolve_selection(theme="light", template="dark")
    assert theme.theme.name == "light"      # explicit theme beat the legacy dark
    assert layout.name == "default"   # layout unspecified, so the legacy value supplied it

    layout, theme = resolve_selection(layout="compliance", template="dark")
    assert layout.name == "compliance"
    assert theme.theme.name == "dark"       # theme unspecified, so the legacy value supplied it


def test_resolve_selection_defaults_with_nothing_supplied():
    layout, theme = resolve_selection()
    assert (layout.name, theme.theme.name) == ("default", "auto")


# --- switchers ---------------------------------------------------------------------------------------

def test_switchers_list_every_layout_and_theme(session_factory):
    html = _render(session_factory)
    assert 'id="layout-select"' in html
    assert 'id="theme-select"' in html
    for lay in list_layouts():
        assert f'value="{lay.name}"' in html
        assert lay.label in html
    for t in list_themes():
        assert f'value="{t.name}"' in html
        assert t.label in html


def test_active_layout_and_theme_are_preselected(session_factory):
    html = _render(session_factory, layout="compliance", theme="dark")
    assert 'value="compliance" selected' in html
    assert 'value="dark" selected' in html
    assert 'value="default" selected' not in html
    assert 'value="auto" selected' not in html


def test_switcher_js_writes_BOTH_axes_before_dropping_the_legacy_parameter(session_factory):
    """A switcher must carry the other axis forward, or it silently discards it.

    The legacy ``?template=`` encodes a Layout AND a Theme together, and the JS deletes it (leaving a
    stale one beside a fresh explicit choice would leave the URL describing two selections). So a
    switcher that wrote only its OWN axis dropped the other: arriving at ``?template=dark`` and
    changing the Layout produced ``?layout=compliance`` with no theme, resolving to ``auto`` — the
    exact bookmarked-URL guarantee ``reporting/selection.py`` promises, broken on the first click.

    Honest about what this asserts: it reads the emitted JS, so it pins the SHAPE of the fix, not its
    runtime behaviour. It is meaningful only in combination with
    ``test_active_layout_and_theme_are_preselected`` (immediately above), which proves the premise the
    fix rests on — both <select>s carry the RESOLVED selection, including one translated out of a
    legacy ``template=``, so reading the other element's value is correct rather than a guess. The
    end-to-end behaviour belongs to the browser suite.
    """
    html = _render(session_factory)
    assert 'u.searchParams.set("layout", layout.value)' in html
    assert 'u.searchParams.set("theme", theme.value)' in html
    assert 'u.searchParams.delete("template")' in html
    # the broken single-axis form must be gone, not merely joined by the new one
    assert "u.searchParams.set(param, el.value)" not in html


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
