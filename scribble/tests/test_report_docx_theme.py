"""The docx route resolves and passes the install-default theme to render_report_docx.

Before this fix the route called render_report_docx with no theme= kwarg, so Synoptek (and any
other install with a default_report_theme) always got the plain default.docx template.
"""

from __future__ import annotations

import io
import uuid
from unittest.mock import patch

import scribble.models as fm
import scribble.report_docx_api as docx_api
from scribble.models import ScribbleSettings, ScribbleThemeOverride

UI = "/scribble"

_SYNOPTEK_TOML = """
[identity]
name = "synoptek"
label = "Synoptek"
stamp = "light"

[tokens]
accent = "#002d62"
"""


def _make_engagement(session_factory) -> uuid.UUID:
    with session_factory() as db:
        eng = fm.ReportBoard(name="Synoptek Eng", client_id=uuid.uuid7())
        db.add(eng)
        db.commit()
        return eng.id


def _seed_synoptek_override(session_factory) -> None:
    with session_factory() as db:
        db.add(ScribbleThemeOverride(name="synoptek", label="Synoptek", source_toml=_SYNOPTEK_TOML))
        db.add(ScribbleSettings(slot="default", default_report_theme="synoptek"))
        db.commit()


def test_docx_route_passes_install_default_theme_to_renderer(client, session_factory):
    """With Synoptek set as install default the docx route must resolve and forward that theme."""
    eng_id = _make_engagement(session_factory)
    _seed_synoptek_override(session_factory)

    captured = {}

    real_render = docx_api.render_report_docx

    def _spy(ctx, *, artifact_bytes=None, body_font=None, code_font=None, theme=None):
        captured["theme"] = theme
        return real_render(ctx, artifact_bytes=artifact_bytes,
                           body_font=body_font, code_font=code_font, theme=None)

    with patch.object(docx_api, "render_report_docx", _spy):
        resp = client.get(f"{UI}/engagements/{eng_id}/report.docx")

    assert resp.status_code == 200
    assert captured.get("theme") is not None, "theme= must be passed (was None)"
    assert captured["theme"].theme.name == "synoptek", (
        f"expected synoptek theme, got {captured['theme'].theme.name!r}"
    )


def test_docx_route_falls_back_to_default_when_no_theme_set(client, session_factory):
    """No ScribbleSettings row → theme resolves to auto (the base default), not an error."""
    eng_id = _make_engagement(session_factory)

    captured = {}
    real_render = docx_api.render_report_docx

    def _spy(ctx, *, artifact_bytes=None, body_font=None, code_font=None, theme=None):
        captured["theme"] = theme
        return real_render(ctx, artifact_bytes=artifact_bytes,
                           body_font=body_font, code_font=code_font, theme=None)

    with patch.object(docx_api, "render_report_docx", _spy):
        resp = client.get(f"{UI}/engagements/{eng_id}/report.docx")

    assert resp.status_code == 200
    assert captured.get("theme") is not None
    assert captured["theme"].theme.name == "auto"
