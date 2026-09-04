"""Report **Layouts** — which blocks a report contains, and in what order.

A Layout is *structure only*. It says "methodology comes before findings"; it says nothing about what
anything looks like. Appearance is a :mod:`scribble.reporting.themes` concern, and the two are
orthogonal on purpose: **any Layout renders under any Theme**.

That orthogonality is the whole reason this module exists. Until #100 a single frozen dataclass carried
both (``ReportTemplate(name, label, theme, blocks)``), so every layout/theme pairing needed its own
registry row — "compliance layout in the firm brand" was unreachable without an N x M table. Splitting
them turns that product into two small sums.

``render_html`` renders exactly the blocks a Layout names, in order, so a Layout can reorder or drop
whole sections without touching the block renderers. This is deliberately data — a frozen registry — so
a future layout *editor* has something concrete to edit; the shipped layouts are just the two here.

Blocks (keys dispatched in ``render_html._render_block_by_key``):

- ``cover``        — print-only title page.
- ``toc``          — print-only table of contents; follows the Layout, so it lists whatever this Layout
                     actually renders, in this Layout's order, without knowing anything about it.
- ``summary``      — Executive Summary (risk banner, narrative, severity bar, metrics, findings index).
- ``findings``     — the filter bar + the finding groups.
- ``diagrams``     — embedded attack-path diagrams.
- ``chains``       — authored attack-chain narratives (#628).
- ``retest``       — remediation closeout: finding → most-recent retest outcome (#622). Renders only when
                     some report-visible finding has a recorded retest, so a report with none is
                     byte-identical to before this block existed.
- ``methodology``  — the standing methodology description + coverage / compliance checklists.
- ``evidence``     — appendix of ENGAGEMENT-level evidence (artifacts with no ``finding_id``).
- ``activity_log`` — optional activity appendix.

``cover`` and ``toc`` are ``display: none`` on screen and shown only in ``@media print``: on screen the
sticky toolbar's section jumps and the "Findings at a glance" index already do this navigation live,
while on paper both of those are gone (``.topbar`` is ``no-print``) — so the printed deliverable is the
only place they add anything.

**Naming.** This module says Layout, never "template". In this extension ``template`` already means the
``scribble_report_templates`` table (operator-uploaded Document Templates, still unused),
``report_templates/default.docx`` (a docxtpl file), and ``VulnerabilityTemplate`` (library boilerplate).
See ``scribble/CONTEXT.md``.
"""

from __future__ import annotations

from dataclasses import dataclass

# Every block key a Layout may reference. Kept here so an unknown key in a Layout is a caught
# programming error, and so a future editor can offer the closed set.
BLOCK_KEYS: tuple[str, ...] = (
    "cover",
    "toc",
    "summary",
    "findings",
    "diagrams",
    "chains",
    "retest",
    "methodology",
    "evidence",
    "activity_log",
)


@dataclass(frozen=True)
class ReportLayout:
    """Which blocks a report contains, in order. Carries no appearance — see ``themes.ReportTheme``."""

    name: str
    label: str
    blocks: tuple[str, ...]

    def __post_init__(self) -> None:
        for b in self.blocks:
            assert b in BLOCK_KEYS, f"unknown block {b!r}"


# ``cover`` then ``toc`` FIRST — a deliverable opens on its title page and its contents, and both carry
# ``break-after: page`` so they own page 1 and page 2 of the PDF. ``diagrams`` sits right AFTER
# ``findings`` (attack-path diagrams are a visual extension of the findings they connect), and
# ``evidence`` sits LAST: it is an appendix of engagement-level material, so it belongs after everything
# else rather than interrupting it.
_STANDARD_BLOCKS = (
    "cover", "toc", "summary", "findings", "diagrams", "chains", "retest", "methodology", "evidence",
)

# Ordered so the switcher lists them predictably; ``default`` is first / the fallback.
_LAYOUTS: tuple[ReportLayout, ...] = (
    ReportLayout("default", "Standard", _STANDARD_BLOCKS),
    # Methodology/coverage BEFORE findings — proves a Layout can reorder whole sections. The TOC follows
    # the Layout, so it lists methodology before the findings here without knowing anything about it.
    # ``diagrams`` still follows ``findings`` here, same rule as the standard Layout.
    ReportLayout(
        "compliance",
        "Compliance-first",
        ("cover", "toc", "summary", "methodology", "findings", "diagrams", "chains", "retest",
         "evidence"),
    ),
)

LAYOUTS: dict[str, ReportLayout] = {layout.name: layout for layout in _LAYOUTS}
DEFAULT_LAYOUT = "default"


def get_layout(name: str | None) -> ReportLayout:
    """Resolve a Layout by name; unknown/blank falls back to ``default``.

    Never raises for callers that pass through an untrusted ``?layout=`` query value.
    """
    return LAYOUTS.get((name or "").strip().lower(), LAYOUTS[DEFAULT_LAYOUT])


def list_layouts() -> list[ReportLayout]:
    """All Layouts in switcher order (default first)."""
    return list(_LAYOUTS)
