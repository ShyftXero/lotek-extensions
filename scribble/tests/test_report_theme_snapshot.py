"""Delivery Snapshot (ext#106): the resolved report Theme is FROZEN onto the Engagement at first
delivery (the first themed export), and from then on every render serves that frozen branding —
regardless of what the install default / override / reader preference / ``?theme=`` would resolve to
now, and even after the source Theme is uninstalled.

The Definition of Done is the first test here: a delivered report re-renders IDENTICALLY after the
install default Theme is changed underneath it. The control test next to it flips the SAME default on an
UNDELIVERED engagement and shows the output DOES change — without it the DoD test would pass even if the
freeze did nothing.

The base stylesheet carries all FOUR palettes at once (light ``:root``, two dark blocks, and the print
block), so a palette's hex values are present in every report no matter which Theme is selected. What a
selected Theme uniquely writes is the ``data-theme`` STAMP on the one ``<html>`` tag — that is the
reliable fingerprint of which Theme actually rendered. (The token PAYLOAD freezing is proven separately,
at the unit level, against ``build_theme_assets``' override-only output, where the base sheet is absent.)
"""

from __future__ import annotations

import uuid

from sqlalchemy import select

import scribble.models as fm
from scribble.models import ScribbleSettings
from scribble.reporting.theme_css import build_theme_assets
from scribble.reporting.theme_registry import (
    freeze_resolved_theme,
    resolve_theme,
    thaw_resolved_theme,
)

UI = "/scribble"

# The one place a selected Theme's identity shows uniquely in the document: the <html> stamp. The bundled
# dark/light Themes stamp differently, so this flips cleanly between them. (The hex palettes do NOT — the
# base sheet carries every palette, so a colour string is present regardless of selection.)
DARK_FINGERPRINT = '<html lang="en" data-theme="dark">'
LIGHT_FINGERPRINT = '<html lang="en" data-theme="light">'


def _make_engagement(session_factory) -> uuid.UUID:
    with session_factory() as db:
        eng = fm.ReportBoard(name="Delivery Snapshot Under Test", scope_type="external", company_name="Acme")
        db.add(eng)
        db.commit()
        return eng.id


def _set_install_default(session_factory, name: str | None) -> None:
    with session_factory() as db:
        settings = db.scalar(select(ScribbleSettings).where(ScribbleSettings.slot == "default"))
        if settings is None:
            settings = ScribbleSettings(slot="default")
            db.add(settings)
        settings.default_report_theme = name
        db.commit()


def _snapshot(session_factory, eid: uuid.UUID):
    with session_factory() as db:
        return db.get(fm.ReportBoard, eid).report_theme_snapshot


# ── the Definition of Done ───────────────────────────────────────────────────────────────────────


def test_delivered_report_re_renders_identically_after_the_install_default_changes(
    client, stub_host, session_factory
):
    eid = _make_engagement(session_factory)

    # Install default is dark at the moment of delivery.
    _set_install_default(session_factory, "dark")
    delivered = client.get(f"{UI}/engagements/{eid}/report/export?format=html")
    assert delivered.status_code == 200
    delivered_html = delivered.get_data(as_text=True)
    assert DARK_FINGERPRINT in delivered_html

    # Delivery froze the snapshot.
    assert _snapshot(session_factory, eid) is not None

    # Now change the install default underneath the delivered report.
    _set_install_default(session_factory, "light")

    # Re-export AND re-open the live view: both must still be the DELIVERED (dark) branding, not light.
    re_exported = client.get(f"{UI}/engagements/{eid}/report/export?format=html").get_data(as_text=True)
    live = client.get(f"{UI}/engagements/{eid}/report").get_data(as_text=True)
    for rendered in (re_exported, live):
        assert DARK_FINGERPRINT in rendered, "the delivered Theme must survive the default change"
        assert LIGHT_FINGERPRINT not in rendered, "the new default must NOT leak into a delivered report"


def test_an_undelivered_report_follows_the_install_default_change(client, stub_host, session_factory):
    """The control: the SAME default flip on an engagement that was never exported DOES change the
    render. Without this the DoD test above would pass even if the snapshot did nothing."""
    eid = _make_engagement(session_factory)

    _set_install_default(session_factory, "dark")
    assert DARK_FINGERPRINT in client.get(f"{UI}/engagements/{eid}/report").get_data(as_text=True)
    assert _snapshot(session_factory, eid) is None  # a live VIEW never freezes

    _set_install_default(session_factory, "light")
    after = client.get(f"{UI}/engagements/{eid}/report").get_data(as_text=True)
    assert LIGHT_FINGERPRINT in after
    assert DARK_FINGERPRINT not in after


# ── immutability & which events count as delivery ─────────────────────────────────────────────────


def test_the_snapshot_is_written_once_and_never_recomputed(client, stub_host, session_factory):
    eid = _make_engagement(session_factory)
    _set_install_default(session_factory, "dark")
    client.get(f"{UI}/engagements/{eid}/report/export?format=html")
    first = _snapshot(session_factory, eid)

    _set_install_default(session_factory, "light")
    client.get(f"{UI}/engagements/{eid}/report/export?format=html")
    assert _snapshot(session_factory, eid) == first, "a second export must not re-snapshot"


def test_a_live_view_is_not_a_delivery(client, stub_host, session_factory):
    eid = _make_engagement(session_factory)
    _set_install_default(session_factory, "dark")
    client.get(f"{UI}/engagements/{eid}/report")
    assert _snapshot(session_factory, eid) is None


def test_structured_data_exports_are_not_a_delivery(client, stub_host, session_factory):
    """csv/json carry no Theme, so they must not freeze one — a later themed export is the real delivery."""
    eid = _make_engagement(session_factory)
    _set_install_default(session_factory, "dark")
    client.get(f"{UI}/engagements/{eid}/report/export?format=json")
    client.get(f"{UI}/engagements/{eid}/report/export?format=csv")
    assert _snapshot(session_factory, eid) is None


# ── fall closed: a snapshot whose Theme was since uninstalled ──────────────────────────────────────


def test_a_snapshot_renders_from_its_own_payload_after_the_theme_is_uninstalled():
    """The payload is self-contained by design: it references no installed Theme, so an uninstalled one
    (modelled by renaming the snapshot to a name nothing resolves) still renders its frozen tokens."""
    snap = freeze_resolved_theme(resolve_theme("dark"))
    # Simulate the source Theme being gone: rename it to something no Provenance can resolve.
    snap["theme"]["name"] = snap["file"]["name"] = "uninstalled-brand"

    # A bare resolve of that name has NO payload (it degrades to auto) — proving the branding below can
    # only be coming from the frozen snapshot, not from re-resolving the name.
    assert resolve_theme("uninstalled-brand").carries_payload is False

    # build_theme_assets' output is the override block ALONE (no base sheet), so here a token value IS an
    # unambiguous fingerprint: dark.toml's sev-high, present only because the snapshot carried it.
    css = build_theme_assets(thaw_resolved_theme(snap)).css
    assert "#ef8a44" in css
