"""lotek#620: manual override of a report's overall risk band.

The computed band (``risk_rating`` ladder) is a FACT; the override is an AUTHORED judgement layered on
top of it. These tests pin:

- **regression** — with NO override the render is unchanged: the "assessor-adjusted" marker never
  appears and the banner shows the computed band (the override code path is dormant with a NULL column);
- **HTML override** — the banner shows the effective band + the marker + the original computed band +
  the rationale;
- **DOCX override** — ``overall_label`` carries the marker + computed band and the summary narrative
  carries the attributed rationale (parity with the HTML banner, no binary-template regen);
- **direction** — an override works BOTH down (Critical→Low) and up (Low→Critical).
"""
from __future__ import annotations

import io
import re
import zipfile

import pytest

from scribble.content import schema
from scribble.enums import Severity
from scribble.models import Engagement, EngagementFinding, FindingGroup
from scribble.reporting import build_report_context
from scribble.reporting.render_docx import render_report_docx
from scribble.reporting.render_html import render_report_html

_RATIONALE = "The lone Critical is not exploitable in this segmented lab; overall exposure is High."


def _block(text: str) -> dict:
    return schema.doc_from_text(text)


def _engagement_with_worst(session_factory, worst: Severity) -> int:
    """An engagement whose single finding fixes the COMPUTED overall band at ``worst``."""
    with session_factory() as db:
        eng = Engagement(name="Override Case", company_name="Acme Corp", scope_type="external")
        grp = FindingGroup(engagement=eng, name="Findings", order_index=0)
        EngagementFinding(
            engagement=eng,
            group=grp,
            title="Primary Finding",
            severity=worst,
            order_index=0,
            content_json={"description": _block("Drives the computed overall band.")},
        )
        db.add(eng)
        db.commit()
        return eng.id


def _set_override(session_factory, eng_id, band: Severity | None, rationale: str | None) -> None:
    with session_factory() as db:
        eng = db.get(Engagement, eng_id)
        eng.risk_override = band
        eng.risk_override_rationale = rationale
        db.commit()


def _render_html(session_factory, eng_id) -> str:
    with session_factory() as db:
        return render_report_html(build_report_context(db.get(Engagement, eng_id)))


def _docx_text(data: bytes) -> str:
    """Flatten ``word/document.xml`` to run-concatenated plain text so a value split across Word runs
    still matches a substring assertion."""
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        xml = z.read("word/document.xml").decode("utf-8")
    return re.sub(r"<[^>]+>", "", xml)


def _render_docx_text(session_factory, eng_id) -> str:
    with session_factory() as db:
        return _docx_text(render_report_docx(build_report_context(db.get(Engagement, eng_id))))


# ── regression: the dormant path ─────────────────────────────────────────────────────────────────


def test_no_override_leaves_the_banner_computed(session_factory):
    eng_id = _engagement_with_worst(session_factory, Severity.critical)
    html = _render_html(session_factory, eng_id)
    # the banner CONTAINER carries the computed band (asserting on the container class, not a bare
    # `risk-critical` substring which the always-inlined stylesheet also contains):
    assert 'class="risk risk-critical"' in html
    # markup-unique tokens (NOT the CSS class names, which the stylesheet always defines):
    assert "⚑ assessor-adjusted" not in html
    assert "computed: Critical" not in html
    assert "Assessor&#39;s rationale" not in html


def test_no_override_leaves_the_docx_computed(session_factory):
    eng_id = _engagement_with_worst(session_factory, Severity.critical)
    text = _render_docx_text(session_factory, eng_id)
    assert "assessor-adjusted" not in text
    assert "computed:" not in text


# ── HTML override render ─────────────────────────────────────────────────────────────────────────


def test_html_override_shows_effective_marker_computed_and_rationale(session_factory):
    eng_id = _engagement_with_worst(session_factory, Severity.critical)
    _set_override(session_factory, eng_id, Severity.high, _RATIONALE)
    html = _render_html(session_factory, eng_id)

    # effective band (High) is the banner CONTAINER class; the computed Critical is NOT anymore
    assert 'class="risk risk-high"' in html
    assert 'class="risk risk-critical"' not in html
    # the marker, the preserved computed band, and the authored rationale are all present
    assert "⚑ assessor-adjusted" in html
    assert "computed: Critical" in html
    assert _RATIONALE in html
    assert "Assessor&#39;s rationale" in html


def test_html_override_without_rationale_omits_the_note(session_factory):
    # The model/PATCH require a rationale; but the RENDERER must not crash if one is somehow absent —
    # it simply omits the rationale block while still marking the adjustment.
    eng_id = _engagement_with_worst(session_factory, Severity.critical)
    _set_override(session_factory, eng_id, Severity.low, None)
    html = _render_html(session_factory, eng_id)
    assert 'class="risk risk-low"' in html
    assert "⚑ assessor-adjusted" in html
    assert "Assessor&#39;s rationale" not in html


# ── DOCX override render ─────────────────────────────────────────────────────────────────────────


def test_docx_override_carries_marker_computed_and_rationale(session_factory):
    eng_id = _engagement_with_worst(session_factory, Severity.critical)
    _set_override(session_factory, eng_id, Severity.high, _RATIONALE)
    text = _render_docx_text(session_factory, eng_id)
    assert "assessor-adjusted" in text
    assert "computed: Critical" in text
    assert "High" in text
    assert _RATIONALE in text


# ── direction: both ways ─────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("computed", "override"),
    [
        (Severity.critical, Severity.low),   # down
        (Severity.low, Severity.critical),   # up
    ],
)
def test_override_direction_both_ways(session_factory, computed, override):
    eng_id = _engagement_with_worst(session_factory, computed)
    _set_override(session_factory, eng_id, override, _RATIONALE)
    html = _render_html(session_factory, eng_id)
    # the banner CONTAINER carries the EFFECTIVE band, and NOT the computed one — asserted on the
    # container class (a bare `risk-<band>` substring is always in the inlined stylesheet, so it would
    # be false-green, and would leave the up-shift case unverified).
    assert f'class="risk risk-{override.value}"' in html
    assert f'class="risk risk-{computed.value}"' not in html
    assert f"computed: {computed.value.title()}" in html
    assert "⚑ assessor-adjusted" in html
