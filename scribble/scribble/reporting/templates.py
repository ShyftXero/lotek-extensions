"""Report layout templates (WS7 / report-vision Phase 5).

A **template** is the ordered list of top-level blocks the HTML report is assembled from, plus a theme.
``render_html`` renders exactly the blocks a template names, in order, so a template can reorder or drop
whole sections without touching the block renderers. This is deliberately data — a frozen registry — so
a future template *editor* (operator-customizable layouts) has something concrete to edit; the shipped
layout is just the ``default`` template.

Blocks (keys dispatched in ``render_html._render_block_by_key``):
- ``cover``       — PRINT-ONLY title page: client, engagement, assessment kind, testing window, assessor,
  report date, the Confidential badge and the handling notice. ``break-after: page``, and it takes the
  masthead's place on paper (``body.has-cover .masthead`` is hidden in ``@media print``).
- ``toc``         — PRINT-ONLY table of contents, DERIVED from the blocks this template renders and the
  groups/findings in the context, so it cannot list a section the document does not have.
- ``summary``     — Executive Summary: front matter (engagement overview + scope and limitations), the
  risk banner, the generated narrative, the severity bar + rating definitions, metrics, findings index.
- ``findings``    — the filter bar + the finding groups.
- ``diagrams``    — linked vector attack-path diagrams (ext#48), each a self-contained HTML snapshot in a
  sandboxed iframe. Renders nothing when the engagement has no linked diagram, which is every report
  today — this is what makes adding the block backward-compatible (see ``render_html._render_diagrams``).
- ``methodology`` — the standing methodology description + coverage / compliance checklists.
- ``evidence``    — appendix of ENGAGEMENT-level evidence (artifacts with no ``finding_id``). Renders
  nothing when there is none, which is the normal case; the toolbar link follows the rendered anchor.

``cover`` and ``toc`` are ``display: none`` on screen and shown only in ``@media print``: on screen the
sticky toolbar's section jumps and the "Findings at a glance" index already do this navigation live, while
on paper both of those are gone (``.topbar`` is ``no-print``) — so the printed deliverable is the only
place they add anything. That also means adding these two blocks changed nothing on screen (the ``summary``
block's front matter is a separate, deliberate on-screen change).

Theme (stamped on ``<html data-theme=…>`` by ``render_html``):
- ``auto``  — no stamp; follows the viewer's ``prefers-color-scheme`` (current default behavior).
- ``light`` — force the light palette.
- ``dark``  — force the dark palette.
"""

from __future__ import annotations

from dataclasses import dataclass

# Every block key a template may reference. Kept here so an unknown key in a template is a caught
# programming error, and so a future editor can offer the closed set.
BLOCK_KEYS: tuple[str, ...] = (
    "cover",
    "toc",
    "summary",
    "findings",
    "diagrams",
    "methodology",
    "evidence",
    "activity_log",
)
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


# ``cover`` then ``toc`` FIRST — a deliverable opens on its title page and its contents, and both carry
# ``break-after: page`` so they own page 1 and page 2 of the PDF. ``diagrams`` sits right AFTER
# ``findings`` (attack-path diagrams are a visual extension of the findings they connect), and
# ``evidence`` sits LAST: it is an appendix of engagement-level material, so it belongs after everything
# else rather than interrupting it.
_STANDARD_BLOCKS = ("cover", "toc", "summary", "findings", "diagrams", "methodology", "evidence")

# Ordered so the switcher lists them predictably; ``default`` is first / the fallback.
_TEMPLATES: tuple[ReportTemplate, ...] = (
    ReportTemplate("default", "Standard", "auto", _STANDARD_BLOCKS),
    # Methodology/coverage BEFORE findings — proves a template can reorder whole sections. The TOC follows
    # the template, so it lists methodology before the findings here without knowing anything about it.
    # ``diagrams`` still follows ``findings`` here, same rule as the standard template.
    ReportTemplate(
        "compliance",
        "Compliance-first",
        "auto",
        ("cover", "toc", "summary", "methodology", "findings", "diagrams", "evidence"),
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
