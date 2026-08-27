"""Resolving a (Layout, Theme) pair from request parameters — including the legacy one.

Before #100 a report was selected with a single ``?template=`` value naming one row of a frozen
registry that carried structure *and* appearance. Three rows existed: ``default``, ``compliance``, and
``dark`` — where ``dark`` was the standard blocks with the dark palette forced. After the split those
three are no longer one axis, so ``?template=`` has to be translated rather than looked up.

**Bookmarked and shared report URLs carry it.** A report is a client deliverable; its URL gets pasted
into tickets and emails, and ``?template=dark`` in one of those must keep producing a dark report
rather than silently reverting to the default. So the legacy parameter is translated, not dropped.

Precedence, deliberately one-directional:

1. An explicit ``layout``/``theme`` wins on its own axis.
2. ``template`` fills in only the axes left unspecified.
3. Anything still unset falls back to ``default`` / ``auto``.

Rule 1 over rule 2 is what lets the switcher work: it sets ``?layout=`` / ``?theme=`` and deletes
``template``, but a stale ``template`` surviving in a hand-edited URL must not veto an explicit choice.
Unknown values on any parameter fall back rather than raising — every one of these arrives untrusted
from a query string.
"""

from __future__ import annotations

from scribble.reporting.layouts import DEFAULT_LAYOUT, ReportLayout, get_layout
from scribble.reporting.themes import DEFAULT_THEME, ReportTheme, get_theme

# Legacy ``?template=`` value -> (layout name, theme name). ``dark`` is the interesting one: it was never
# a distinct structure, only the standard blocks with a forced palette, which is exactly the conflation
# the split undoes.
_LEGACY: dict[str, tuple[str, str]] = {
    "default": (DEFAULT_LAYOUT, DEFAULT_THEME),
    "compliance": ("compliance", DEFAULT_THEME),
    "dark": (DEFAULT_LAYOUT, "dark"),
}


def resolve_selection(
    *,
    layout: str | None = None,
    theme: str | None = None,
    template: str | None = None,
) -> tuple[ReportLayout, ReportTheme]:
    """Resolve the Layout and Theme to render with. See this module's docstring for precedence.

    Every argument is untrusted; an unrecognised value falls back instead of raising.
    """
    legacy = _LEGACY.get((template or "").strip().lower())
    if legacy is not None:
        legacy_layout, legacy_theme = legacy
        # An explicit value on an axis wins; the legacy value fills only what was left unspecified.
        layout = layout or legacy_layout
        theme = theme or legacy_theme
    return get_layout(layout), get_theme(theme)
