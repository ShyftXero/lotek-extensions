"""Report layout templates (WS7 / report-vision Phase 5).

A **template** is the ordered list of top-level blocks the HTML report is assembled from, plus a theme.
``render_html`` renders exactly the blocks a template names, in order, so a template can reorder or drop
whole sections without touching the block renderers. This is deliberately data — a frozen registry — so
a future template *editor* (operator-customizable layouts) has something concrete to edit; the shipped
layout is just the ``default`` template.

Blocks (keys dispatched in ``render_html._render_block_by_key``):
- ``summary``     — Executive Summary (risk banner, narrative, severity bar, metrics, findings index).
- ``findings``    — the filter bar + the finding groups.
- ``methodology`` — the standing methodology description + coverage / compliance checklists.
- ``evidence``    — appendix of ENGAGEMENT-level evidence (artifacts with no ``finding_id``). Renders
  nothing when there is none, which is the normal case; the toolbar link follows the rendered anchor.

Theme (stamped on ``<html data-theme=…>`` by ``render_html``):
- ``auto``  — no stamp; follows the viewer's ``prefers-color-scheme`` (current default behavior).
- ``light`` — force the light palette.
- ``dark``  — force the dark palette.
"""

from __future__ import annotations

from dataclasses import dataclass

# Every block key a template may reference. Kept here so an unknown key in a template is a caught
# programming error, and so a future editor can offer the closed set.
BLOCK_KEYS: tuple[str, ...] = ("summary", "findings", "methodology", "evidence")
THEMES: tuple[str, ...] = ("auto", "light", "dark")


@dataclass(frozen=True)
class ReportTemplate:
    name: str
    label: str
    theme: str
    blocks: tuple[str, ...]

    def __post_init__(self) -> None:
        assert self.theme in THEMES, f"unknown theme {self.theme!r}"
        for b in self.blocks:
            assert b in BLOCK_KEYS, f"unknown block {b!r}"


# ``evidence`` sits LAST in every shipped template: it is an appendix of engagement-level material, so it
# belongs after the findings and the methodology rather than interrupting either.
_STANDARD_BLOCKS = ("summary", "findings", "methodology", "evidence")

# Ordered so the switcher lists them predictably; ``default`` is first / the fallback.
_TEMPLATES: tuple[ReportTemplate, ...] = (
    ReportTemplate("default", "Standard", "auto", _STANDARD_BLOCKS),
    # Methodology/coverage BEFORE findings — proves a template can reorder whole sections.
    ReportTemplate(
        "compliance", "Compliance-first", "auto", ("summary", "methodology", "findings", "evidence")
    ),
    # Same layout, dark theme forced — proves a template can carry theme.
    ReportTemplate("dark", "Dark", "dark", _STANDARD_BLOCKS),
)

TEMPLATES: dict[str, ReportTemplate] = {t.name: t for t in _TEMPLATES}
DEFAULT_TEMPLATE = "default"


def get_template(name: str | None) -> ReportTemplate:
    """Resolve a template by name; unknown/blank falls back to ``default`` (never raises for callers
    that pass through an untrusted ``?template=`` query value)."""
    return TEMPLATES.get((name or "").strip().lower(), TEMPLATES[DEFAULT_TEMPLATE])


def list_templates() -> list[ReportTemplate]:
    """All templates in switcher order (default first)."""
    return list(_TEMPLATES)
