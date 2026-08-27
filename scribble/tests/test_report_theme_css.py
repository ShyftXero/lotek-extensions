"""The Theme -> CSS integration seam (`reporting/theme_css.py`).

The modules this joins were each proven in isolation. What these tests pin is the thing none of them
could check alone: that a Theme's Tokens land in the right PLACE in the cascade.

Two failure modes are guarded, both invisible on screen:

1. A plain `:root { }` override (0-1-0) loses to `[data-theme="dark"]` (0-2-0), so a Theme would
   silently not apply to a dark-stamped report.
2. Reaching paper when the Theme never asked to. A Theme's `[tokens]` are SCREEN values; the
   `@media print` rule in `_CSS` forces paper-appropriate values for every colour token, because a
   dark-mode browser once printed near-white ink and a 1.6:1 accent onto white paper. Paper theming
   is therefore opt-in via `[print_tokens]`.
"""

from __future__ import annotations

import re

import pytest

from scribble.reporting.theme_css import build_theme_assets
from scribble.reporting.themes import get_theme


def _css_for(theme_name: str) -> str:
    return build_theme_assets(get_theme(theme_name)).css


def _block_after(css: str, at_rule: str) -> str:
    """The declaration block nested inside a given @media rule."""
    m = re.search(re.escape(at_rule) + r"\s*\{(.*?)\n\}", css, re.S)
    return m.group(1) if m else ""


@pytest.mark.parametrize("theme_name", ["light", "dark"])
def test_a_bundled_theme_emits_a_screen_override(theme_name):
    assert "@media screen {" in _css_for(theme_name)


@pytest.mark.parametrize("theme_name", ["light", "dark"])
def test_a_theme_without_print_tokens_leaves_paper_alone(theme_name):
    """THE regression guard. Neither bundled Theme declares [print_tokens], so neither may emit a
    print override — their [tokens] are SCREEN values. An earlier cut of this module carried
    "brand identity" tokens (accent, severity ramp, type) to paper automatically, and the dark
    Theme's screen orange #ef8a44 duly printed where the paper ramp uses #c2410c."""
    assert "@media print" not in _css_for(theme_name)


@pytest.mark.parametrize("theme_name", ["light", "dark"])
def test_the_override_is_0_2_0_so_it_beats_the_dark_rule(theme_name):
    """`:root:root` ties the 0-2-0 of `[data-theme="dark"]` and comes later in the sheet, so it wins.
    A bare `:root` (0-1-0) would silently lose to it, and nothing on screen would say so."""
    css = _css_for(theme_name)
    assert ":root:root {" in css
    assert not re.search(r"(?<!:root)\n:root \{", css)


@pytest.mark.parametrize("theme_name", ["light", "dark"])
def test_never_important(theme_name):
    """`!important` would also beat the print rule, reintroducing paper theming through the back door
    for a Theme that never declared paper values."""
    assert "!important" not in _css_for(theme_name)


@pytest.mark.parametrize("theme_name", ["light", "dark"])
def test_screen_receives_the_full_token_set(theme_name):
    from scribble.reporting.theme_files import load_theme_file

    loaded = load_theme_file(theme_name)
    assert loaded is not None
    screen_block = _block_after(_css_for(theme_name), "@media screen")
    for token in loaded.tokens:
        assert f"--{token}:" in screen_block


def test_a_theme_that_declares_print_tokens_does_reach_paper(monkeypatch):
    """The opt-in path a brand Theme will use: declare paper values, having reasoned about contrast on
    white, and they are emitted at a specificity that beats the base print rule."""
    from scribble.reporting import theme_css as tc
    from scribble.reporting.theme_files import _parse_theme_toml

    toml = """
[identity]
name = "papered"
label = "Papered"

[tokens]
accent = "#123456"

[print_tokens]
accent = "#0a5b3d"
"""
    parsed = _parse_theme_toml("papered", toml)
    assert parsed.print_tokens == {"accent": "#0a5b3d"}

    monkeypatch.setattr(tc.theme_files, "load_theme_file", lambda name: parsed)
    css = tc.build_theme_assets(get_theme("dark")).css

    assert "--accent: #0a5b3d;" in _block_after(css, "@media print")
    assert "--accent: #123456;" in _block_after(css, "@media screen")


def test_a_broken_theme_degrades_unthemed_but_LOUDLY(monkeypatch, caplog):
    """Degrading safely and degrading silently are two different decisions; only the first is wanted.

    A Theme that fails to load still renders a perfectly clean report — just an unbranded one — so a
    swallowed exception here means a client receives a wrong-looking deliverable with nothing raised,
    nothing logged, and nothing in the UI. That is the exact behaviour `theme_discovery` was forbidden
    from copying, and INV-EXT-05 requires a denial to be loud.
    """
    from scribble.reporting import theme_css as tc

    def boom(name):
        raise ValueError("malformed TOML")

    monkeypatch.setattr(tc.theme_files, "load_theme_file", boom)
    with caplog.at_level("WARNING"):
        assets = tc.build_theme_assets(get_theme("dark"))

    assert assets.css == ""  # degrades to the base sheet, never to a 500
    assert any("failed to load" in r.getMessage() for r in caplog.records), (
        "the failure must be logged, not swallowed"
    )


def test_auto_is_not_logged_as_a_failure(caplog):
    """`auto` having no bundled file is by DESIGN, not an error — logging it would train the operator
    to ignore the very warning that matters."""
    with caplog.at_level("WARNING"):
        build_theme_assets(get_theme("auto"))
    assert not caplog.records


def test_css_carrying_a_style_close_is_refused(monkeypatch):
    """Defense in depth at the point the CSS becomes part of an HTML document. Unreachable through the
    current grammar, but the payload is slated to start arriving from an operator (override
    provenance), and a breakout would put chosen markup inside a document embedding client evidence."""
    from scribble.reporting import theme_css as tc

    monkeypatch.setattr(tc.theme_files, "build_font_face_css", lambda theme: "</style><script>x</script>")
    css = tc.build_theme_assets(get_theme("dark")).css
    assert css == ""
    assert "</style" not in css.lower()


def test_auto_theme_contributes_nothing():
    """`auto` has no bundled file by design — it IS the base stylesheet's own behaviour, so there is
    nothing to override and the report must come out byte-identical to an unthemed one."""
    assets = build_theme_assets(get_theme("auto"))
    assert assets.css == ""
    assert assets.is_empty


def test_an_unknown_theme_degrades_to_the_base_sheet():
    """get_theme falls back to auto for anything unrecognised, and a Theme that cannot be resolved
    must degrade to the shipped appearance rather than to a broken page."""
    assert build_theme_assets(get_theme("does-not-exist")).css == ""


def test_the_report_carries_the_override_after_the_base_sheet(session_factory):
    """End to end: the rendered document must contain a SECOND <style> holding the override, and it
    must come after the base sheet or the cascade argument does not hold."""
    from scribble.models import Engagement
    from scribble.reporting import build_report_context
    from scribble.reporting.render_html import render_report_html

    with session_factory() as db:
        eng = Engagement(name="Themed Co Assessment", company_name="Themed Co")
        db.add(eng)
        db.commit()
        eid = eng.id
    with session_factory() as db:
        html = render_report_html(build_report_context(db.get(Engagement, eid)), theme="dark")

    assert html.count("<style>") >= 2
    assert ":root:root {" in html
    assert html.index(":root:root {") > html.index("@media print")  # after the base sheet


def test_an_unthemed_report_gains_no_second_style(session_factory):
    """The auto path must not start emitting an empty <style> — that would be a silent diff on every
    existing report."""
    from scribble.models import Engagement
    from scribble.reporting import build_report_context
    from scribble.reporting.render_html import render_report_html

    with session_factory() as db:
        eng = Engagement(name="Plain Co Assessment", company_name="Plain Co")
        db.add(eng)
        db.commit()
        eid = eng.id
    with session_factory() as db:
        html = render_report_html(build_report_context(db.get(Engagement, eid)))

    assert html.count("<style>") == 1
    assert ":root:root {" not in html
