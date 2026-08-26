"""Schema for the per-install default Theme and the per-engagement Report Theme Snapshot (#100, #105,
#106).

Covers the two schema elements #105/#106 add, per ``scribble/CONTEXT.md``'s vocabulary:

- ``ScribbleSettings`` -- a singleton row (mirrors CREAM's ``Brand``) carrying the install-wide default
  Theme name. Exactly one row can ever exist.
- ``Engagement.report_theme`` -- the chosen Theme name, nullable ("inherit the install default").
- ``Engagement.report_theme_snapshot`` -- the resolved tokens/marks frozen at delivery, and specifically
  that it survives a LATER change to the install default unchanged -- the entire property the Snapshot
  concept (see CONTEXT.md) exists to buy: a report already in a client's hands must not silently
  re-theme itself because someone edited an install-wide setting afterwards.

No route/API surface exists yet (that is the orchestrator's), so these tests talk to the ORM directly.
"""

from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError

from scribble.models import Engagement, ScribbleSettings

# --- ScribbleSettings: singleton -----------------------------------------------------------------------


def test_settings_singleton_rejects_a_second_default_slot(session_factory):
    with session_factory() as db:
        db.add(ScribbleSettings(slot="default", default_report_theme="dark"))
        db.commit()

    with session_factory() as db:
        db.add(ScribbleSettings(slot="default", default_report_theme="light"))
        with pytest.raises(IntegrityError):
            db.commit()


def test_default_report_theme_round_trips(session_factory):
    with session_factory() as db:
        db.add(ScribbleSettings(slot="default", default_report_theme="dark"))
        db.commit()

    with session_factory() as db:
        settings = db.query(ScribbleSettings).filter_by(slot="default").one()
        assert settings.default_report_theme == "dark"


def test_default_report_theme_is_nullable():
    """NULL is a meaningful state here (see ``ScribbleSettings`` docstring), not just "unconfigured" —
    the model must accept it rather than require a default Theme to be chosen up front."""
    settings = ScribbleSettings(slot="default")
    assert settings.default_report_theme is None


# --- Engagement.report_theme ---------------------------------------------------------------------------


def test_engagement_report_theme_defaults_to_null(session_factory):
    with session_factory() as db:
        eng = Engagement(name="Acme Q3")
        db.add(eng)
        db.commit()
        eng_id = eng.id

    with session_factory() as db:
        reloaded = db.get(Engagement, eng_id)
        assert reloaded.report_theme is None  # "inherit the install default"
        assert reloaded.report_theme_snapshot is None  # nothing frozen yet


def test_engagement_report_theme_round_trips(session_factory):
    with session_factory() as db:
        eng = Engagement(name="Acme Q3", report_theme="dark")
        db.add(eng)
        db.commit()
        eng_id = eng.id

    with session_factory() as db:
        reloaded = db.get(Engagement, eng_id)
        assert reloaded.report_theme == "dark"


# --- Engagement.report_theme_snapshot ------------------------------------------------------------------

_SNAPSHOT_PAYLOAD = {
    "theme": "dark",
    "tokens": {
        "colors": {"primary": "#0f766e", "accent": "#f59e0b", "background": "#0b0f14"},
        "type": {"body": "Inter, system-ui, sans-serif", "heading": "Georgia, serif"},
        "radius": "6px",
    },
    "marks": {"logo": "data:image/png;base64,AAAA", "shapes": ["chevron"]},
}


def test_snapshot_stores_and_returns_a_nested_dict_unchanged(session_factory):
    with session_factory() as db:
        eng = Engagement(name="Acme Q3", report_theme_snapshot=_SNAPSHOT_PAYLOAD)
        db.add(eng)
        db.commit()
        eng_id = eng.id

    with session_factory() as db:
        reloaded = db.get(Engagement, eng_id)
        assert reloaded.report_theme_snapshot == _SNAPSHOT_PAYLOAD


def test_snapshot_survives_the_install_default_changing(session_factory):
    """The property the Snapshot exists for: a Report already delivered must keep rendering with the
    tokens it was ISSUED with, not whatever the install default happens to be today."""
    with session_factory() as db:
        db.add(ScribbleSettings(slot="default", default_report_theme="light"))
        eng = Engagement(name="Acme Q3", report_theme_snapshot=_SNAPSHOT_PAYLOAD)
        db.add(eng)
        db.commit()
        eng_id = eng.id

    # The install default changes AFTER delivery -- an admin re-themes the whole install.
    with session_factory() as db:
        settings = db.query(ScribbleSettings).filter_by(slot="default").one()
        settings.default_report_theme = "dark"
        db.commit()

    # The already-delivered engagement's frozen payload must be untouched by that change.
    with session_factory() as db:
        reloaded = db.get(Engagement, eng_id)
        assert reloaded.report_theme_snapshot == _SNAPSHOT_PAYLOAD
        settings = db.query(ScribbleSettings).filter_by(slot="default").one()
        assert settings.default_report_theme == "dark"  # sanity: the install default really did move
