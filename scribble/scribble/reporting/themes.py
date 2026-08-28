"""Report **Themes** — how a report looks: palette, typefaces, marks.

A Theme is *appearance only*. It cannot add, remove, or reorder a block; that is a
:mod:`scribble.reporting.layouts` concern. Any Layout renders under any Theme.

A Theme's ``stamp`` is written onto ``<html data-theme=…>`` by ``render_html``, which is what selects a
palette out of the stylesheet:

- ``auto``  — stamps nothing; the report follows the viewer's ``prefers-color-scheme``.
- ``light`` — forces the light palette.
- ``dark``  — forces the dark palette.

Note that the base stylesheet carries **four** palettes, not three: ``:root`` (light), the
``prefers-color-scheme: dark`` block, ``[data-theme="dark"]``, and a further set inside ``@media
print``. The print block's selectors deliberately match every case — no stamp, ``light``, and ``dark``
— so paper always gets paper colours regardless of the Theme. Anything that later overrides tokens
per-Theme (#101) has to compose with all four, or a themed report prints unthemed.

**Today a Theme is identity plus a stamp, and nothing more.** The token payload — the colours, type
stacks, and marks a Theme actually carries, and the closed allowlist that validates them — lands in
#101; Marks land in #104. This module exists first so that the Layout/Theme split is complete and
provable before anything depends on the payload's shape.

``light`` is selectable here for the first time. It was always a legal value of the old
``THEMES`` tuple, but no entry in the frozen registry used it, so the only way to get a
guaranteed-light report was to be a viewer whose OS was set to light.
"""

from __future__ import annotations

from dataclasses import dataclass

# The values ``<html data-theme>`` may carry. Empty string means "stamp nothing" (follow the viewer).
STAMPS: tuple[str, ...] = ("", "light", "dark")


@dataclass(frozen=True)
class ReportTheme:
    """How a report looks. Carries no structure — see ``layouts.ReportLayout``."""

    name: str
    label: str
    stamp: str

    def __post_init__(self) -> None:
        assert self.stamp in STAMPS, f"unknown stamp {self.stamp!r}"

    @property
    def html_attr(self) -> str:
        """The ``data-theme`` attribute to splice into the ``<html>`` tag, or ``""`` for auto."""
        return f' data-theme="{self.stamp}"' if self.stamp else ""


# Ordered so the switcher lists them predictably; ``auto`` is first / the fallback, preserving the
# behaviour every report had before Themes were selectable.
_THEMES: tuple[ReportTheme, ...] = (
    ReportTheme("auto", "Auto (system)", ""),
    ReportTheme("light", "Light", "light"),
    ReportTheme("dark", "Dark", "dark"),
)

THEMES: dict[str, ReportTheme] = {t.name: t for t in _THEMES}
DEFAULT_THEME = "auto"


def get_theme(name: str | None) -> ReportTheme:
    """Resolve a Theme by name; unknown/blank falls back to ``auto``.

    Never raises for callers that pass through an untrusted ``?theme=`` query value.
    """
    return THEMES.get((name or "").strip().lower(), THEMES[DEFAULT_THEME])


def list_themes() -> list[ReportTheme]:
    """All Themes in switcher order (auto first)."""
    return list(_THEMES)
