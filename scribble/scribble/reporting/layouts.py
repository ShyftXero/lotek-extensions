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
- ``strategic``    — authored strategic (longer-horizon) recommendations (#623). Renders only when the
                     engagement carries at least one, so a report with none is byte-identical to before.
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
    "rollups",
    "findings",
    "diagrams",
    "chains",
    "retest",
    "strategic",
    "methodology",
    "evidence",
    "activity_log",
)

# Human-readable labels for each block — the names a section-order editor shows and the TOC/handles use.
# Keyed by BLOCK_KEYS; kept beside the keys so adding a block forces adding its label (asserted below).
BLOCK_LABELS: dict[str, str] = {
    "cover": "Cover",
    "toc": "Table of Contents",
    "summary": "Executive Summary",
    "rollups": "Findings Rollups",
    "findings": "Findings",
    "diagrams": "Attack-Path Diagrams",
    "chains": "Attack Chains",
    "retest": "Remediation Retest",
    "strategic": "Strategic Recommendations",
    "methodology": "Methodology",
    "evidence": "Evidence Appendix",
    "activity_log": "Activity Log",
}
assert set(BLOCK_LABELS) == set(BLOCK_KEYS), "BLOCK_LABELS must label exactly BLOCK_KEYS"


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
    "cover", "toc", "summary", "rollups", "findings", "diagrams", "chains", "retest", "strategic",
    "methodology", "evidence",
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
        ("cover", "toc", "summary", "methodology", "rollups", "findings", "diagrams", "chains", "retest",
         "strategic", "evidence"),
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


# --------------------------------------------------------------------------- per-report section order
#
# A named Layout (above) is a shipped PRESET. A per-report section order is the operator's own arrangement,
# persisted on ``ReportBoard.section_order`` and resolved here for BOTH renderers so HTML and the DOCX/PDF
# deliverable agree. The preset only seeds/previews; this is the persisted truth.

# The default composition when a report has no saved order: every block, standard order, all enabled EXCEPT
# the opt-in ``activity_log`` appendix (off by default keeps today's output — no shipped layout referenced
# it). Ordered = _STANDARD_BLOCKS then activity_log last; asserted to cover the whole vocabulary so adding a
# BLOCK_KEY forces placing it here.
DEFAULT_SECTION_ORDER: tuple[str, ...] = (*_STANDARD_BLOCKS, "activity_log")
_DEFAULT_DISABLED: frozenset[str] = frozenset({"activity_log"})
assert set(DEFAULT_SECTION_ORDER) == set(BLOCK_KEYS), "DEFAULT_SECTION_ORDER must cover BLOCK_KEYS"


@dataclass(frozen=True)
class SectionSpec:
    """One report section in the resolved order: its block key + whether it renders. ``label`` is the
    human name (BLOCK_LABELS) for the editor UI."""

    key: str
    enabled: bool

    @property
    def label(self) -> str:
        return BLOCK_LABELS[self.key]


def default_section_specs() -> list[SectionSpec]:
    """The full section list in default order — every block, all enabled but the opt-in activity_log."""
    return [SectionSpec(k, k not in _DEFAULT_DISABLED) for k in DEFAULT_SECTION_ORDER]


def resolve_section_order(raw: object) -> list[SectionSpec]:
    """Resolve ``ReportBoard.section_order`` (a JSON list, or None) into the ordered, validated section
    specs BOTH renderers loop.

    ``raw`` items may be ``{"key": ..., "enabled": bool}`` or a bare key string (enabled). Robust to
    anything persisted or POSTed: an unknown key is dropped, a duplicate collapses to its first
    appearance, and any BLOCK_KEY absent from ``raw`` is appended at the end **disabled** — so a report
    saved before a new block existed never silently gains it, yet the editor still lists it to turn on.
    ``None``/empty → :func:`default_section_specs`.
    """
    if not raw or not isinstance(raw, list):
        return default_section_specs()
    seen: set[str] = set()
    specs: list[SectionSpec] = []
    for item in raw:
        if isinstance(item, str):
            key, enabled = item, True
        elif isinstance(item, dict):
            key = item.get("key")
            enabled = bool(item.get("enabled", True))
        else:
            continue
        if key in BLOCK_KEYS and key not in seen:
            seen.add(key)
            specs.append(SectionSpec(key, enabled))
    for key in DEFAULT_SECTION_ORDER:  # append any block the saved order never mentioned, OFF
        if key not in seen:
            specs.append(SectionSpec(key, False))
    return specs


def enabled_section_keys(raw: object) -> tuple[str, ...]:
    """The block keys that actually render, in order — the resolved order with disabled sections removed.
    This is what a renderer loops."""
    return tuple(s.key for s in resolve_section_order(raw) if s.enabled)
