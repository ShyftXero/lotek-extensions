"""Self-contained HTML report renderer (WS7).

Consumes ``ReportContext`` (the FROZEN contract in ``scribble.reporting.context``) only — no DB access,
no Flask. Produces a standalone HTML document: its own inline ``<style>``/``<script>``, no external
hosts, so it is deliverable on its own and prints cleanly to PDF. Aesthetic matches Lotek's
``report.html`` (dark theme, ``--accent`` green, ``--sev-*`` ramp, collapsible sections, gradient
header) using Scribble's own token values (``scribble/static/scribble.css``).

Rendering honors board order == document order: it iterates ``ctx.groups`` and each group's
``findings`` exactly as given (already ordered + filtered by ``build_report_context``); this module
does no re-sorting or re-filtering of its own (the client-side "sort by severity" control is a
non-destructive, print-safe *view* toggle only — see the inline JS).

Asset handling
--------------
``ReportContext.groups[].findings[].artifacts`` (the evidence gallery) carries real ``storage_path``
values per attached artifact. Content blocks (``blocks_html``) may additionally contain inline
``<img class="artifact" src="...">`` tags for pasted-in-line images; those come from
``content/render_html.py``'s ``inlineImage`` node, whose ``src`` is whatever the ``artifact_url``
callback passed to ``build_report_context`` returned. To make inline images embeddable here too
(without ``ReportContext`` needing to expose inline-placement artifacts, which are intentionally out
of its frozen shape), callers should build the context with ``artifact_url=`` wired to
:func:`make_inline_artifact_url`, which bakes the artifact's ``storage_path`` straight into a
placeholder URL. That placeholder is a schemeless relative path, so it survives
``content/render_html.py``'s stricter sanitize pass (no ``data:`` allowed there — see that module's
docstring); this module then resolves the placeholder to a real ``data:`` URI (or a relative
``artifacts/...`` path for :func:`export_zip`) and re-sanitizes *only that small fragment* with nh3,
explicitly allowing the ``data:`` scheme. ``content/render_html.py`` itself is never weakened.
"""

from __future__ import annotations

import hashlib
import io
import mimetypes
import re
import zipfile
from collections.abc import Callable
from datetime import UTC, datetime
from html import escape as _escape
from urllib.parse import quote, unquote

import nh3

from scribble.enums import SEVERITY_ORDER as _ENUM_SEVERITY_ORDER
from scribble.reporting.context import ArtifactCtx, FindingCtx, GroupCtx, ReportContext
from scribble.reporting.templates import ReportTemplate, get_template, list_templates

ArtifactBytes = Callable[[str], "bytes | None"]

SEVERITY_ORDER: tuple[str, ...] = tuple(s.value for s in _ENUM_SEVERITY_ORDER)  # worst-first

_BLOCK_LABELS = {"description": "Description", "remediation": "Remediation", "details": "Details"}
_BLOCK_ORDER = ("description", "remediation", "details")

# Toolbar label per top-level block key (``reporting.templates.BLOCK_KEYS``). A key is linked only when
# its block actually rendered its ``id="sec-<key>"`` anchor — see ``_render_document``/``_render_header``.
# ``cover`` and ``toc`` are deliberately ABSENT: both are print-only (``display: none`` on screen), so a
# toolbar link would scroll a reader to an invisible element — the same class of broken link as ext#42's
# link into an empty anchor, which is why the on-screen toolbar must not offer them at all.
_NAV_LABELS = {
    "summary": "Summary",
    "findings": "Findings",
    "methodology": "Methodology",
    "evidence": "Evidence",
}

# A 1x1 transparent GIF — the fallback ``src`` for an inline image that can't be resolved (no bytes
# available / not embedding), so the document never emits an empty/broken external-looking src.
_BLANK_PIXEL = "data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw=="

# Placeholder scheme baked into inline-image src by ``make_inline_artifact_url`` / resolved by
# ``_substitute_inline_placeholders``. Relative + schemeless so it survives the stricter sanitize pass
# in ``content/render_html.py`` (which only allows nh3's default url schemes).
_INLINE_PREFIX = "/__scribble_inline__/"
_INLINE_SRC_RE = re.compile(re.escape(_INLINE_PREFIX) + r"([A-Za-z0-9%_.~-]+)")

# Narrow allowlist for the small HTML fragments *this module* builds itself (evidence gallery items),
# sanitized separately from — and more permissively (data: URIs) than — content/render_html.py.
_ASSET_TAGS = {"figure", "figcaption", "a", "img", "div", "span"}
_ASSET_ATTRS = {
    "a": {"href", "download", "class", "id"},
    "img": {"src", "alt", "class", "loading"},
    "div": {"class"},
    "span": {"class"},
    "figure": {"class"},
    "figcaption": {"class"},
}
_ASSET_URL_SCHEMES = {"http", "https", "mailto", "data"}


def make_inline_artifact_url(storage_path: str | None) -> str:
    """Build the placeholder ``src`` for an inline (``inlineImage``) content node.

    Encodes ``storage_path`` directly into the URL so this module can resolve it to a real asset later
    without a separate id -> storage_path lookup at render time. Pass this as (or wire it into) the
    ``artifact_url`` callback given to ``build_report_context`` for inline images to be embeddable by
    :func:`render_report_html` / :func:`export_zip`.
    """
    if not storage_path:
        return ""
    return _INLINE_PREFIX + quote(storage_path, safe="")


def _esc(value: str | None) -> str:
    return _escape(value or "", quote=True)


def _safe_href(url: str | None) -> str | None:
    """Only render a target URL as a clickable link if it has a safe, explicit scheme."""
    if url and url.startswith(("http://", "https://")):
        return url
    return None


def _data_uri(content_type: str | None, data: bytes) -> str:
    import base64

    mime = _escape(content_type or "application/octet-stream")
    b64 = base64.b64encode(data).decode("ascii")
    return f"data:{mime};base64,{b64}"


def _safe_name_from_path(storage_path: str) -> str:
    """A collision-resistant, path-traversal-safe basename for a ZIP ``artifacts/`` entry."""
    base = storage_path.replace("\\", "/").rsplit("/", 1)[-1]
    base = "".join(c for c in base if c.isalnum() or c in "._-") or "asset"
    digest = hashlib.sha1(storage_path.encode("utf-8")).hexdigest()[:8]
    return f"{digest}-{base}"


def _fetch_data_uri(
    artifact_bytes: ArtifactBytes | None, storage_path: str | None, content_type: str | None
) -> str | None:
    if not artifact_bytes or not storage_path:
        return None
    try:
        data = artifact_bytes(storage_path)
    except Exception:
        return None
    if not data:
        return None
    mime = content_type or mimetypes.guess_type(storage_path)[0]
    return _data_uri(mime, data)


class _AssetResolver:
    """Resolves evidence-gallery + inline-content assets to hrefs for one render pass.

    ``mode="inline"``: embed real bytes as ``data:`` URIs (self-contained report).
    ``mode="zip"``:    point at a relative ``artifacts/<name>`` path and record it in ``manifest`` so
                       :func:`export_zip` can write the bytes alongside ``report.html``.
    ``mode="none"``:   no bytes available/wanted — degrade gracefully (missing-asset placeholder /
                       blank pixel). Used for structure-only rendering (e.g. tests, previews).
    """

    def __init__(self, mode: str, artifact_bytes: ArtifactBytes | None):
        self.mode = mode
        self.artifact_bytes = artifact_bytes
        self.manifest: dict[str, str] = {}  # safe_name -> storage_path (zip mode)

    def resolve_gallery(self, artifact: ArtifactCtx) -> str | None:
        if self.mode == "inline":
            return _fetch_data_uri(self.artifact_bytes, artifact.storage_path, artifact.content_type)
        if self.mode == "zip":
            name = _safe_name_from_path(f"{artifact.id}/{artifact.filename}")
            self.manifest[name] = artifact.storage_path
            return f"artifacts/{name}"
        return None

    def resolve_inline(self, storage_path: str) -> str:
        if self.mode == "inline":
            return _fetch_data_uri(self.artifact_bytes, storage_path, None) or _BLANK_PIXEL
        if self.mode == "zip":
            name = _safe_name_from_path(storage_path)
            self.manifest[name] = storage_path
            return f"artifacts/{name}"
        return _BLANK_PIXEL


def _sanitize_asset_html(fragment: str) -> str:
    return nh3.clean(fragment, tags=_ASSET_TAGS, attributes=_ASSET_ATTRS, url_schemes=_ASSET_URL_SCHEMES)


def _substitute_inline_placeholders(fragment: str, resolver: _AssetResolver) -> str:
    if _INLINE_PREFIX not in fragment:
        return fragment

    def _sub(m: re.Match[str]) -> str:
        storage_path = unquote(m.group(1))
        return resolver.resolve_inline(storage_path)

    return _INLINE_SRC_RE.sub(_sub, fragment)


def _render_gallery_item(artifact: ArtifactCtx, resolver: _AssetResolver) -> str:
    href = resolver.resolve_gallery(artifact)
    cap = _esc(artifact.caption or artifact.filename)
    is_image = (artifact.content_type or "").startswith("image/")
    if href and is_image:
        raw = (
            f'<figure class="evidence-item">'
            f'<a class="evidence-link" href="#ev-{artifact.id}">'
            f'<img src="{href}" alt="{cap}" loading="lazy"/></a>'
            f"<figcaption>{cap}</figcaption></figure>"
            f'<a class="lightbox" id="ev-{artifact.id}" href="#_" aria-label="close">'
            f'<img src="{href}" alt="{cap}"/></a>'
        )
    elif href:
        raw = (
            f'<div class="evidence-item file"><a class="file-chip" href="{href}" '
            f'download="{_esc(artifact.filename)}">\U0001f4c4 {_esc(artifact.filename)}</a>'
            f'<div class="cap">{cap}</div></div>'
        )
    else:
        raw = (
            f'<div class="evidence-item file missing">\U0001f4c4 {_esc(artifact.filename)} '
            f'<span class="cap">(not embedded)</span></div>'
        )
    return _sanitize_asset_html(raw)


def _render_gallery(f: FindingCtx, resolver: _AssetResolver) -> str:
    return _render_artifact_gallery(f.artifacts, resolver)


def _render_artifact_gallery(
    artifacts: list[ArtifactCtx], resolver: _AssetResolver, *, label: str | None = "Evidence"
) -> str:
    """An evidence grid for any artifact list. ``label=None`` drops the "Evidence (n)" caption, for a
    gallery rendered somewhere that already says what it is (a child instance's table cell)."""
    if not artifacts:
        return ""
    items = "".join(_render_gallery_item(a, resolver) for a in artifacts)
    cap = f'<div class="evidence-label">{_esc(label)} ({len(artifacts)})</div>' if label else ""
    return f'<div class="evidence">{cap}<div class="evidence-grid">{items}</div></div>'


def _affected_assets(f: FindingCtx) -> list[tuple[str, str | None]]:
    """``(label, href)`` for every asset this finding touches: its own target host/url, any per-host
    child instances, and an ``AFFECTED`` variable overlay. De-duplicated by label, order-preserving.
    ``href`` is set only for a safe-scheme URL so the renderer can link it."""
    out: list[tuple[str, str | None]] = []
    seen: set[str] = set()

    def add(label: str, href: str | None = None) -> None:
        label = (label or "").strip()
        if label and label not in seen:
            seen.add(label)
            out.append((label, href))

    if f.target_host:
        add(f.target_host + (f":{f.target_port}" if f.target_port else ""))
    if f.target_url:
        add(f.target_url, _safe_href(f.target_url))
    for c in f.children:
        add(_child_host_label(c))
    affected = f.variables.get("AFFECTED") if isinstance(f.variables, dict) else None
    if affected:
        for piece in str(affected).replace(";", ",").split(","):
            add(piece.strip())
    return out


def _render_affected_assets(f: FindingCtx) -> str:
    """Always-present per-finding "Affected Assets" section (empty-state when nothing is recorded)."""
    assets = _affected_assets(f)
    if not assets:
        body = '<span class="muted">Not specified.</span>'
    else:
        lis = []
        for label, href in assets:
            inner = f'<a href="{_esc(href)}">{_esc(label)}</a>' if href else _esc(label)
            lis.append(f"<li>{inner}</li>")
        body = f'<ul class="asset-list">{"".join(lis)}</ul>'
    return (
        '<div class="block affected-assets"><div class="block-label">Affected Assets</div>'
        f'<div class="block-body">{body}</div></div>'
    )


def _render_recommendations(f: FindingCtx, resolver: _AssetResolver) -> str:
    """Always-present per-finding "Recommendations" section, from the ``remediation`` content block
    (empty-state prompt when unauthored)."""
    fragment = f.blocks_html.get("remediation")
    if fragment:
        body = _substitute_inline_placeholders(fragment, resolver)
    else:
        body = '<span class="muted">No recommendation recorded.</span>'
    return (
        '<div class="block recommendations"><div class="block-label">Recommendations</div>'
        f'<div class="block-body">{body}</div></div>'
    )


def _render_blocks(f: FindingCtx, resolver: _AssetResolver) -> str:
    """The finding's descriptive content blocks (description, details, any custom blocks) -- NOT
    ``remediation``, which renders separately as the always-present Recommendations section."""
    parts: list[str] = []
    seen: set[str] = {"remediation"}  # rendered separately by _render_recommendations
    for key in _BLOCK_ORDER:
        if key in seen:
            continue
        seen.add(key)
        fragment = f.blocks_html.get(key)
        if fragment:
            parts.append(_render_block(key, fragment, resolver))
    for key, fragment in f.blocks_html.items():
        if key in seen or not fragment:
            continue
        parts.append(_render_block(key, fragment, resolver))
    return "\n".join(parts)


def _render_block(key: str, fragment: str, resolver: _AssetResolver) -> str:
    label = _BLOCK_LABELS.get(key, key.replace("_", " ").title())
    resolved = _substitute_inline_placeholders(fragment, resolver)
    return (
        f'<div class="block"><div class="block-label">{_esc(label)}</div>'
        f'<div class="block-body">{resolved}</div></div>'
    )


def _child_host_label(c: FindingCtx) -> str:
    if c.target_host:
        return c.target_host + (f":{c.target_port}" if c.target_port else "")
    if c.target_url:
        return c.target_url
    return c.title


def _child_summary_text(c: FindingCtx) -> str:
    """The per-host evidence line for a child finding's row in the compact "Affected hosts" table --
    built from the child's OWN ``variables`` overlay (``FindingCtx.facts_line``, e.g. ``"cheddarsale.local
    — svc_sql, svc_web"``), not from its content blocks: every child promoted under the same vuln-DB
    template shares that template's ``content_json`` verbatim with its parent (see
    ``scribble.promote.promote_job``), so a per-child excerpt of ``blocks_html`` would just repeat the
    parent's write-up for every host rather than showing what's actually different about this one."""
    return c.facts_line


def _render_child_evidence_cell(c: FindingCtx, resolver: _AssetResolver) -> str:
    """The Evidence cell for one child instance: its per-host facts line AND its own attached artifacts.

    The gallery half is ext#40: ``_render_finding`` gives a top-level finding's artifacts a gallery, but a
    CHILD was only ever rendered through this table, whose Evidence column was the facts line alone. A
    screenshot attached to a promoted per-host instance therefore produced an empty cell — and promoted
    scan findings are exactly where nesting comes from (``scribble.promote.promote_job``), so per-host
    evidence was the case most likely to be lost. An em-dash keeps a genuinely empty cell scannable
    instead of blank."""
    facts = _child_summary_text(c)
    gallery = _render_artifact_gallery(c.artifacts, resolver, label=None)
    if not facts and not gallery:
        return '<span class="muted">—</span>'
    facts_html = f'<div class="child-facts">{_esc(facts)}</div>' if facts else ""
    return facts_html + gallery


def _render_children(f: FindingCtx, resolver: _AssetResolver) -> str:
    """A COMPACT per-host list for a parent finding's children -- rendered once, collapsed by default,
    instead of one full finding card per instance (see module docstring header)."""
    if not f.children:
        return ""
    rows = "".join(
        f'<tr><td class="child-host">{_esc(_child_host_label(c))}</td>'
        f'<td class="child-evidence">{_render_child_evidence_cell(c, resolver)}</td></tr>'
        for c in f.children
    )
    n = len(f.children)
    return (
        f'<details class="children"><summary>Affected hosts ({n})</summary>'
        '<table class="children-table"><thead><tr><th>Host</th><th>Evidence</th></tr></thead>'
        f"<tbody>{rows}</tbody></table></details>"
    )


def _render_finding(f: FindingCtx, resolver: _AssetResolver) -> str:
    sev = f.severity
    rank = SEVERITY_ORDER.index(sev) if sev in SEVERITY_ORDER else len(SEVERITY_ORDER)
    badges = f'<span class="sev-badge sev-{_esc(sev)}">{_esc(sev.title())}</span>'
    if f.cvss_score is not None:
        title_attr = f' title="{_esc(f.cvss_vector)}"' if f.cvss_vector else ""
        badges += f'<span class="chip cvss"{title_attr}>CVSS {f.cvss_score:.1f}</span>'
    body = (
        _render_blocks(f, resolver)
        + _render_affected_assets(f)
        + _render_recommendations(f, resolver)
    )
    return (
        f'<article class="finding sev-{_esc(sev)}" data-sev="{_esc(sev)}" data-sevrank="{rank}" '
        f'id="finding-{f.id}">'
        f'<div class="finding-head"><h3>{_esc(f.title)}</h3>'
        f'<div class="finding-badges">{badges}</div></div>'
        f'<div class="finding-body">{body}</div>'
        f"{_render_gallery(f, resolver)}"
        f"{_render_children(f, resolver)}"
        "</article>"
    )


def _group_anchor(group: GroupCtx) -> str:
    """The ``id`` a group's section carries, and therefore the TOC's link target for it (ext#43). Groups
    had ``data-group-id`` but no ``id`` — nothing to link a table of contents at — so this is the one
    piece of new markup the TOC needed. Built here and used by both sides (``_render_group`` and
    ``_toc_entries``) so a renamed anchor can never leave the contents pointing at nothing."""
    return f"group-{group.id if group.id is not None else 'ungrouped'}"


def _render_group(group: GroupCtx, resolver: _AssetResolver) -> str:
    gid = group.id if group.id is not None else "ungrouped"
    swatch = f'<span class="swatch" style="background:{_esc(group.color)}"></span>' if group.color else ""
    n = len(group.findings)
    findings_html = "".join(_render_finding(f, resolver) for f in group.findings) or (
        '<p class="empty">No findings in this section.</p>'
    )
    return (
        f'<section class="sec group" id="{_esc(_group_anchor(group))}" '
        f'data-group-id="{_esc(str(gid))}">'
        f'<h2 class="sec-h">{swatch}{_esc(group.name)} <span class="chev">▾</span>'
        f'<span class="count">{n} finding{"s" if n != 1 else ""}</span></h2>'
        f'<div class="sec-body"><div class="findings-list">{findings_html}</div></div>'
        "</section>"
    )


def _render_filter_bar(ctx: ReportContext) -> str:
    if not any(g.findings for g in ctx.groups):
        return ""
    chips = "".join(
        f'<span class="chip-toggle sev-{s} on" data-sev="{s}" role="button" tabindex="0">{s.title()}</span>'
        for s in SEVERITY_ORDER
    )
    return (
        '<div class="filters no-print" id="finding-filters">'
        f"{chips}"
        '<input type="search" id="finding-search" placeholder="Filter findings (title, host, text)…" '
        'aria-label="Filter findings"/>'
        '<label class="sort-toggle"><input type="checkbox" id="sort-severity"/> Sort by severity</label>'
        '<span class="filter-count" id="finding-count"></span>'
        "</div>"
    )


def _render_header(
    ctx: ReportContext,
    template: ReportTemplate,
    *,
    nav_keys: tuple[str, ...] = (),
    engagement_url: str | None = None,
    dashboard_url: str | None = None,
) -> str:
    """A sticky command bar (back-links + section jumps + layout switcher + print/expand/collapse) over
    a flat, hairline-ruled masthead. All on-screen chrome carries ``no-print`` so the PDF opens straight
    on the masthead.

    ``nav_keys`` are the block keys that actually rendered an ``id="sec-<key>"`` anchor into THIS
    document, in document order — see ``_render_document``. Section links are emitted for those and only
    those: a link to an anchor that isn't in the document reads as a broken report to the client clicking
    it (ext#42, where a checklist-less engagement kept a live "Methodology" link to an empty anchor)."""
    opts = "".join(
        f'<option value="{_esc(t.name)}"{" selected" if t.name == template.name else ""}>'
        f"{_esc(t.label)}</option>"
        for t in list_templates()
    )
    switcher = (
        '<label class="tmpl-switch" title="Report layout template">'
        f'<span>Layout</span><select id="template-select" aria-label="Report layout">{opts}</select></label>'
    )
    eyebrow = _esc(ctx.client_name or ctx.company_name or "Security Assessment")
    # Same helpers the cover page uses, so the two title blocks cannot describe the engagement differently.
    sub_bits = [f"<span>{_assessment_kind(ctx)}</span>"]
    window = _date_range(ctx)
    if window:
        sub_bits.append('<span class="dot">·</span>')
        sub_bits.append(f"<span>{window}</span>")
    assessor = ctx.variables.get("ASSESSOR", "") if ctx.variables else ""
    if assessor:
        sub_bits.append('<span class="dot">·</span>')
        sub_bits.append(f"<span>Assessor: {_esc(assessor)}</span>")

    # Back-links live in the TOOLBAR, not in the masthead (ext#45). They are navigation, and the
    # masthead is the document's own title block — client eyebrow, report title, dates, assessor,
    # Confidential — which is also what a cover page is built from. Wedging app chrome above the client's
    # name is what the client saw and complained about.
    back_links: list[str] = []
    if dashboard_url:
        back_links.append(f'<a href="{_esc(dashboard_url)}">← Dashboard</a>')
    if engagement_url:
        back_links.append(f'<a href="{_esc(engagement_url)}">← Back to engagement</a>')
    back_html = f'<nav class="report-nav no-print">{"".join(back_links)}</nav>' if back_links else ""

    section_links = "".join(
        f'<a href="#sec-{key}">{_esc(_NAV_LABELS[key])}</a>' for key in nav_keys if key in _NAV_LABELS
    )
    section_nav = f'<nav class="tb-sections">{section_links}</nav>' if section_links else ""

    topbar = (
        '<div class="topbar no-print"><div class="wrap">'
        '<div class="tb-brand"><span class="tb-mark"></span> Scribble</div>'
        f"{back_html}"
        f"{section_nav}"
        f'<div class="tb-actions">{switcher}'
        '<button class="btn ghost" type="button" data-action="expand-all">Expand all</button>'
        '<button class="btn ghost" type="button" data-action="collapse-all">Collapse all</button>'
        '<button class="btn primary" type="button" data-action="print">⎙ Print / PDF</button>'
        "</div></div></div>"
    )
    masthead = (
        '<header class="masthead"><div class="wrap">'
        '<div class="row"><div>'
        f'<div class="eyebrow">{eyebrow}</div><h1>{_esc(ctx.engagement_name)}</h1>'
        f'<div class="sub">{"".join(sub_bits)}</div></div>'
        '<span class="confidential">Confidential</span></div></div></header>'
    )
    return topbar + masthead


_SEV_LABELS = {"critical": "Critical", "high": "High", "medium": "Medium", "low": "Low", "info": "Info"}

# ── front matter (ext#43) ─────────────────────────────────────────────────────────────────────────────
#
# STANDING, TEMPLATE-LEVEL boilerplate — the same category as ``_METHODOLOGY_PHASES``, and for the same
# reason: there is no field anywhere in the model for an engagement overview, a scope statement or a
# handling notice, so the only honest place for this prose today is one module constant per piece. A
# per-engagement, editable prose block is a schema + editor change and is deliberately NOT half-built here
# (see plans/fix-scribble-report-render-sweep.md, "Deferred"). Everything below is true of how this
# practice works, names no tool and no host, and states no per-engagement fact: every engagement-specific
# sentence on the cover and in the overview is built from ``ReportContext`` fields and omitted when the
# field is empty, so the report never asserts something nobody recorded.
_COVER_HANDLING = (
    "This report describes weaknesses that could be used against the environment it covers. Treat it as "
    "confidential: share it only with people who need it in order to act on the findings, and store and "
    "transmit it accordingly."
)

# EVERY bullet here states what this report does NOT claim. That constraint is deliberate and is what
# makes the block safe to emit unconditionally: under-claiming cannot be false, whereas an assertion that
# particular work WAS performed can be — and this block is emitted from inside ``_render_summary``, so
# unlike the methodology block it cannot even be dropped without dropping the Executive Summary.
# A third bullet used to read "Testing was non-destructive ... anything outside the agreed scope was not
# touched." It was removed (adversarial review, 2026-08-17): scribble's headline workflow bulk-promotes a
# whole scan job, so for a real deliverable that sentence asserted process facts nobody recorded, in a
# document signed with the assessor's name. Rules-of-engagement statements of that kind belong in the
# per-engagement prose field that is still to be built, written by the assessor — not in a renderer
# constant. ``tests/test_report_standing_prose.py`` pins the rule so it cannot drift back in.
_LIMITATIONS: tuple[str, ...] = (
    "This report describes the environment as it was during the testing window. A change made after "
    "testing — a deployment, a configuration change, a newly published vulnerability — is not reflected "
    "here.",
    "The absence of a finding means a weakness was not observed in the time and scope available. It is "
    "not proof that none exists.",
    "Coverage was bounded by the agreed scope and testing window. Systems, accounts and techniques "
    "outside them were not examined, and this report makes no claim about them.",
)

# What each severity level MEANS, so the severity bar and the findings index are legible to a reader who
# did not run the assessment — the client's "three plain columns of numbers" was partly this: the ratings
# were shown and never defined. Ordered by ``SEVERITY_ORDER`` at render time, not here.
_SEVERITY_DEFINITIONS: dict[str, str] = {
    "critical": (
        "Leads directly and reliably to full compromise of a system, an account of consequence, or a "
        "sensitive data set. Remediate immediately."
    ),
    "high": (
        "Gives an attacker a serious foothold or access to sensitive data without unusual preconditions. "
        "Remediate urgently."
    ),
    "medium": (
        "Real exposure that needs a further condition to be useful — a valid account, user interaction, "
        "or adjacent network access. Remediate on the normal maintenance cycle."
    ),
    "low": (
        "Limited direct impact on its own; most useful to an attacker in combination with something else."
    ),
    "info": "No direct impact. Recorded for completeness, or as hardening worth doing.",
}


def _assessment_kind(ctx: ReportContext) -> str:
    """The one-line description of what this assessment was, for the masthead and the cover page. Renders
    ``scope_type`` VERBATIM (never title-cased) — see ``_render_summary``: a group named after a scope word
    must stay the document's first capitalized occurrence of it (tests/test_report_html.py)."""
    return f"{_esc(ctx.scope_type)} assessment" if ctx.scope_type else "Penetration test report"


def _date_range(ctx: ReportContext) -> str:
    if not (ctx.start_date or ctx.end_date):
        return ""
    return f'{_esc(ctx.start_date or "?")} – {_esc(ctx.end_date or "present")}'


def _cover_facts(ctx: ReportContext) -> list[tuple[str, str]]:
    """The cover page's fact table — ONLY rows whose data the engagement actually carries. A cover page is
    the one part of a deliverable a client reads as a statement of record, so an empty field is omitted
    rather than printed as "—" (which reads as "recorded as nothing")."""
    facts: list[tuple[str, str]] = []
    client = ctx.client_name or ctx.company_name
    if client:
        facts.append(("Client", _esc(client)))
    if ctx.scope_type:
        facts.append(("Assessment type", _esc(ctx.scope_type)))
    window = _date_range(ctx)
    if window:
        facts.append(("Testing window", window))
    assessor = (ctx.variables.get("ASSESSOR", "") if ctx.variables else "") or ""
    if assessor:
        facts.append(("Assessor", _esc(assessor)))
    facts.append(("Report date", datetime.now(UTC).strftime("%Y-%m-%d")))
    facts.append(("Engagement reference", f"#{_esc(str(ctx.engagement_id))}"))
    return facts


def _render_cover(ctx: ReportContext) -> str:
    """The PRINT-ONLY cover page (ext#43): the PDF used to open straight into the masthead and then the
    executive summary, with no title page at all.

    It is a re-layout of front matter the context already carries — client, engagement name, assessment
    kind, window, assessor, the Confidential badge — plus the standing handling notice. On paper it
    REPLACES the masthead (``body.has-cover .masthead`` is hidden in ``@media print``), which is not only
    de-duplication: the masthead is a ``<header>`` *before* ``<main>``, so leaving it visible would print
    it ahead of the cover and the deliverable would still not open on its title page.

    The big title is a ``<div>``, not a second ``<h1>``: the masthead's ``<h1>`` remains the document's
    one top-level heading (the cover is ``display: none`` on screen, so a screen reader never reaches it,
    and Chrome's print path does not carry heading semantics into the PDF anyway).
    """
    facts = "".join(
        f'<div class="cover-fact"><dt>{_esc(label)}</dt><dd>{value}</dd></div>'
        for label, value in _cover_facts(ctx)
    )
    eyebrow = _esc(ctx.client_name or ctx.company_name or "Security Assessment")
    return (
        '<section class="cover" id="sec-cover">'
        '<div class="cover-top">'
        f'<div class="cover-eyebrow">{eyebrow}</div>'
        f'<div class="cover-title">{_esc(ctx.engagement_name)}</div>'
        f'<div class="cover-kind">{_assessment_kind(ctx)}</div>'
        "</div>"
        f'<dl class="cover-facts">{facts}</dl>'
        '<div class="cover-foot">'
        '<span class="cover-badge">Confidential</span>'
        f'<p class="cover-handling">{_esc(_COVER_HANDLING)}</p>'
        "</div></section>"
    )


def _overview_paragraph(ctx: ReportContext) -> str:
    """The engagement-overview sentences: what this report covers, over what window, by whom — assembled
    from ``ReportContext`` fields, one clause per field, each omitted when the field is empty. This is the
    factual half of the overview; ``ctx.narrative`` (generated from the rollup) follows it."""
    company = ctx.company_name or ctx.client_name
    subject = f"of {_esc(company)}" if company else "of the target environment"
    kind = f"{_esc(ctx.scope_type)} assessment" if ctx.scope_type else "security assessment"
    # "an internal assessment", not "a internal assessment" -- ``scope_type`` is operator-supplied free
    # text, so the article has to follow the value rather than being baked into the sentence.
    article = "an" if kind[:1].lower() in "aeiou" else "a"
    bits = [f"This report covers {article} {kind} {subject}."]
    window = _date_range(ctx)
    if window:
        bits.append(f"Testing was carried out over {window}.")
    assessor = (ctx.variables.get("ASSESSOR", "") if ctx.variables else "") or ""
    if assessor:
        bits.append(f"The assessment was performed by {_esc(assessor)}.")
    return " ".join(bits)


def _render_front_matter(ctx: ReportContext) -> str:
    """Front matter at the head of the Executive Summary: the engagement overview and the standing scope /
    limitations statement.

    ext#43's third part, reported as "the executive summary reads like a printed dashboard". It was
    accurate: the summary opened on a risk banner, one generated sentence, a severity bar, three metric
    tiles and an index — all true, and none of it telling a reader what was assessed or what the report
    does and does not claim. This block is prose FIRST, with the dashboard below it as supporting detail.
    """
    # ``_esc`` even though these are module constants, matching ``_methodology_prose``: the escaping is
    # what makes editing the prose safe for someone who reaches for an "&" or an angle bracket.
    limits = "".join(f"<li>{_esc(text)}</li>" for text in _LIMITATIONS)
    narrative = f'<p class="summary-narrative">{_esc(ctx.narrative)}</p>' if ctx.narrative else ""
    return (
        '<div class="frontmatter">'
        '<div class="fm-block"><h3>Engagement overview</h3>'
        f'<p class="fm-lead">{_overview_paragraph(ctx)}</p>{narrative}</div>'
        '<div class="fm-block"><h3>Scope and limitations</h3>'
        f'<ul class="fm-limits">{limits}</ul></div>'
        "</div>"
    )


def _render_severity_definitions(rollup) -> str:
    """What the severity ratings mean, directly under the severity bar they explain. Empty when there are no
    findings — same rule as ``_sev_bar``: a clean report has no ratings to explain."""
    if not rollup or rollup.total <= 0:
        return ""
    rows = "".join(
        f'<div class="sd-row"><span class="sev-tag sev-{s}">{_esc(_SEV_LABELS.get(s, s.title()))}</span>'
        f'<span class="sd-text">{_esc(_SEVERITY_DEFINITIONS[s])}</span></div>'
        for s in SEVERITY_ORDER
        if s in _SEVERITY_DEFINITIONS
    )
    return (
        '<div class="sev-defs"><div class="cap">How these ratings are used</div>'
        f"{rows}</div>"
    )


def _sev_bar(rollup) -> str:
    """A single stacked severity-distribution bar + legend from the rollup counts -- the summary's one
    chart. Empty string when there are no findings (a clean report shows no bar)."""
    counts = rollup.counts if rollup else {}
    total = rollup.total if rollup else 0
    if total <= 0:
        return ""
    segs, legend = [], []
    for s in SEVERITY_ORDER:
        c = counts.get(s, 0)
        label = _SEV_LABELS.get(s, s.title())
        legend.append(
            f'<span class="item"><span class="sw" style="background:var(--sev-{s})"></span>'
            f"{label} <b>{c}</b></span>"
        )
        if c > 0:
            segs.append(f'<div class="seg sev-{s}" style="flex:{c}" title="{label}: {c}">{c}</div>')
    return (
        '<div class="sevbar-wrap"><div class="sevbar-head">'
        '<span class="t">Findings by severity</span>'
        f'<span class="total"><b>{total}</b> total findings</span></div>'
        f'<div class="sevbar">{"".join(segs)}</div>'
        f'<div class="sevlegend">{"".join(legend)}</div></div>'
    )


def _findings_index(ctx: ReportContext) -> str:
    """A scan-then-jump index of every top-level finding (severity · title→its card · host · CVSS),
    in board order. Nested children stay out of this list, matching the finding cards below."""
    rows = []
    for group in ctx.groups:
        for f in group.findings:
            host = f.target_host or ""
            if host and f.target_port:
                host = f"{host}:{f.target_port}"
            if not host and f.target_url:
                host = f.target_url
            cvss = f"{f.cvss_score:.1f}" if f.cvss_score is not None else "—"
            sev, sev_label = _esc(f.severity), _esc(f.severity.title())
            rows.append(
                "<tr>"
                f'<td class="ix-sev"><span class="sev-tag sev-{sev}">{sev_label}</span></td>'
                f'<td class="ix-title"><a href="#finding-{f.id}">{_esc(f.title)}</a></td>'
                f'<td class="ix-host">{_esc(host) or "—"}</td>'
                f'<td class="ix-cvss">{_esc(cvss)}</td></tr>'
            )
    if not rows:
        return ""
    return (
        '<div class="index-wrap"><div class="cap">Findings at a glance</div>'
        '<table class="index"><thead><tr><th>Severity</th><th>Finding</th>'
        '<th>Host</th><th style="text-align:right">CVSS</th></tr></thead>'
        f'<tbody>{"".join(rows)}</tbody></table></div>'
    )


def _render_summary(ctx: ReportContext) -> str:
    rollup = ctx.rollup
    counts = rollup.counts if rollup else {}
    total = rollup.total if rollup else 0
    overall = rollup.overall if rollup else "info"
    clean = total == 0
    banner_class = "risk-clean" if clean else f"risk-{overall}"
    banner_label = "No Findings" if clean else overall.title()
    # A short, factual severity breakdown for the banner subline -- distinct from the (longer) generated
    # ``summary-narrative`` paragraph below, so the two don't repeat each other.
    parts = [f"{counts.get(s, 0)} {s}" for s in SEVERITY_ORDER if counts.get(s, 0)]
    banner_sub = " · ".join(parts) if parts else "No findings within the tested scope."

    n_groups = len(ctx.groups)
    # Render the scope verbatim (not title-cased): a group named after a scope word (e.g. "External")
    # must remain the document's first capitalized occurrence of it, so the group-order contract in
    # tests/test_report_html.py (idx "Internal" < idx "External") holds.
    scope = _esc(ctx.scope_type) if ctx.scope_type else "—"
    dates = ""
    if ctx.start_date or ctx.end_date:
        dates = f'{_esc(ctx.start_date or "?")} – {_esc(ctx.end_date or "present")}'
    date_metric = (
        f'<div class="metric"><div class="k">Window</div><div class="v">{dates}</div></div>' if dates else ""
    )
    # Front matter FIRST (ext#43): the overview prose — which carries ``ctx.narrative`` — then the risk
    # verdict, then the chart, its rating definitions, the metric tiles and the index.
    return (
        '<section class="sec" id="sec-summary">'
        '<h2 class="sec-h">Executive Summary <span class="chev">▾</span></h2>'
        '<div class="sec-body">'
        f"{_render_front_matter(ctx)}"
        f'<div class="risk {banner_class}">'
        '<div class="rating"><div class="label">Overall Risk</div>'
        f'<div class="level">{_esc(banner_label)}</div></div>'
        f'<div class="narr">{_esc(banner_sub)}</div></div>'
        f"{_sev_bar(rollup)}"
        f"{_render_severity_definitions(rollup)}"
        '<div class="metrics">'
        f'<div class="metric"><div class="k">Total Findings</div><div class="v">{total}</div></div>'
        f'<div class="metric"><div class="k">Sections</div><div class="v">{n_groups}</div></div>'
        f'<div class="metric"><div class="k">Scope</div><div class="v">{scope}</div></div>'
        f"{date_metric}"
        "</div>"
        f"{_findings_index(ctx)}"
        "</div></section>"
    )


def _render_groups(ctx: ReportContext, resolver: _AssetResolver) -> str:
    if not ctx.groups:
        return (
            '<section class="sec"><div class="sec-body">'
            '<p class="empty">No findings recorded for this engagement.</p></div></section>'
        )
    return "\n".join(_render_group(g, resolver) for g in ctx.groups)


_BUCKET_ORDER = ("satisfied", "deficient", "not_applicable", "open")


def _rollup_chips(rollup: dict) -> str:
    labels = {"satisfied": "Satisfied", "deficient": "Deficient", "not_applicable": "N/A", "open": "Open"}
    return "".join(
        f'<span class="ck-chip ck-{b}">{labels[b]}: {rollup.get(b, 0)}</span>'
        for b in _BUCKET_ORDER
        if rollup.get(b, 0)
    )


def _finding_link(item) -> str:
    if item.finding_id and item.finding_title:
        return f' <a class="ck-finding" href="#finding-{item.finding_id}">see: {_esc(item.finding_title)}</a>'
    return ""


def _render_checklist_coverage(cl) -> str:
    rows: list[str] = []
    last_section: object = object()
    for it in cl.items:
        if it.section != last_section:
            last_section = it.section
            if it.section:
                rows.append(f'<div class="ck-section">{_esc(it.section)}</div>')
        badge = f'<span class="ck-badge ck-{it.bucket}">{_esc(it.status or it.bucket_label)}</span>'
        note = f'<div class="ck-note">{_esc(it.note)}</div>' if it.note else ""
        rows.append(
            f'<div class="ck-item">{badge}<div class="ck-item-body">'
            f'<div class="ck-item-text">{_esc(it.text)}{_finding_link(it)}</div>{note}</div></div>'
        )
    return (
        f'<article class="ck-list"><div class="ck-head"><h3>{_esc(cl.name)}</h3>'
        f'<div class="ck-rollup">{_rollup_chips(cl.rollup)}</div></div>{"".join(rows)}</article>'
    )


def _render_checklist_compliance(cl) -> str:
    by_fw: dict[str, list] = {}
    fw_order: list[str] = []
    for it in cl.items:
        fw = it.framework or ""
        if fw not in by_fw:
            by_fw[fw] = []
            fw_order.append(fw)
        by_fw[fw].append(it)
    parts = [
        f'<article class="ck-list"><div class="ck-head"><h3>{_esc(cl.name)}</h3>'
        f'<div class="ck-rollup">{_rollup_chips(cl.rollup)}</div></div>'
    ]
    for fw in fw_order:
        if fw:
            parts.append(f'<div class="ck-section">{_esc(fw)}</div>')
        parts.append(
            '<div class="ck-tablewrap"><table class="ck-table"><thead><tr><th>Control</th>'
            "<th>Requirement</th><th>Result</th><th>Notes</th></tr></thead><tbody>"
        )
        for it in by_fw[fw]:
            result = f'<span class="ck-badge ck-{it.bucket}">{_esc(it.bucket_label)}</span>'
            notes = _esc(it.note or "") + _finding_link(it)
            parts.append(
                f'<tr><td class="ck-ctrl">{_esc(it.control_ref or "")}</td>'
                f"<td>{_esc(it.text)}</td><td>{result}</td><td>{notes}</td></tr>"
            )
        parts.append("</tbody></table></div>")
    parts.append("</article>")
    return "".join(parts)


# The standing methodology description: what the assessment process WAS, in phases, plus how severity is
# arrived at. Always rendered (a coverage checklist ADDS its record below it, and is not a substitute for
# describing the approach), because the alternative shipped for months and is what a client complained
# about: the whole Methodology section silently absent, with a live toolbar link into an empty anchor
# (ext#42). Deliberately says nothing about which specific tools ran or which hosts were touched — that is
# per-engagement fact and belongs to the findings, the coverage checklist and the scope statement, not to
# standing boilerplate.
# PRESENT TENSE, throughout, and that is not a style choice — see ``tests/test_report_standing_prose.py``.
# These phases describe the METHOD an assessment of this kind is conducted by. They used to be written as
# past-tense assertions about the engagement in hand ("Every candidate weakness was validated by hand
# before it was reported"), which the renderer cannot know: a report assembled by ``promote_job`` from a
# scan job's findings had the document assert hand-validation of all forty of them. Describing the standard
# method is honest and is what a Methodology section is for; asserting unrecorded work is neither, and no
# operator control exists to remove it (the report-template registry is frozen data, not an editor).
_METHODOLOGY_PHASES: tuple[tuple[str, str], ...] = (
    (
        "Scoping and rules of engagement",
        "The in-scope assets, the testing window and the permitted level of intrusiveness are agreed "
        "before testing begins, and testing stays inside them.",
    ),
    (
        "Reconnaissance and enumeration",
        "In-scope assets are enumerated to establish what is actually reachable and what each exposed "
        "service claims to be — the inventory every later phase is measured against.",
    ),
    (
        "Vulnerability identification",
        "Reachable services and applications are examined for known-vulnerable versions, weak "
        "configuration and exposed functionality, using tooling to widen coverage rather than to decide "
        "the result.",
    ),
    (
        "Validation",
        "Candidate weaknesses are validated before they are reported. Where a finding is carried over "
        "from tool output it is reported with the evidence that supports it and with the assessor's "
        "rating rather than the tool's, so a reader can see what a given finding rests on.",
    ),
    (
        "Controlled exploitation and impact assessment",
        "Where the rules of engagement permit it, a validated weakness is exercised only as far as is "
        "needed to establish its real impact — kept to the minimum that demonstrates the consequence, "
        "and short of destructive action.",
    ),
    (
        "Rating and reporting",
        "Each finding is rated on impact and exploitability in the context of this environment. Where a "
        "CVSS vector is available it is recorded on the finding; the severity shown is the assessor's "
        "rating for this environment, which may differ from a vendor's generic score.",
    ),
)

# Per-assessment-type framing, keyed on ``GroupCtx.type_slug`` — rendered only for the types this
# engagement's report sections actually carry, so the section describes THIS report rather than a menu of
# everything Scribble can do. Prose condensed from skill/scribble-report-refine/references/methodology.md
# (the same framing the report-refine skill writes narratives against, so the two cannot contradict).
_METHODOLOGY_FRAMING: dict[str, tuple[str, str]] = {
    "internal": (
        "Internal network",
        "An internal assessment simulates an attacker who already has a foothold inside the network — a "
        "compromised workstation, a malicious insider, an implant dropped by a phishing pretext "
        "elsewhere. Findings in this section are framed as lateral movement and blast radius: what such "
        "an attacker could reach, escalate to, or pivot through once already inside the perimeter.",
    ),
    "external": (
        "External perimeter",
        "An external assessment simulates an anonymous attacker on the open internet with no prior "
        "access. Findings in this section are framed as initial access: what is reachable and "
        "unauthenticated from outside, and what exploiting it hands an attacker as a first foothold.",
    ),
    "web-app": (
        "Web application",
        "A web application assessment targets the application's own logic and trust boundaries rather "
        "than network-level exposure — authentication and authorization, injection, business-logic abuse, "
        "client-side trust. Findings in this section are framed as what a legitimate-looking request can "
        "be made to do that the application's designers did not intend.",
    ),
    "device-mobile": (
        "Device and mobile",
        "A device or mobile assessment covers a physical device and its companion application — local "
        "storage, platform permission boundaries, on-device secrets, inter-process communication, "
        "physical tampering. Findings in this section are framed as what someone holding the device can "
        "extract or bypass, rather than what is reachable over the network.",
    ),
}


def _methodology_prose(ctx: ReportContext) -> str:
    """The standing methodology description + framing for each assessment type present in this report."""
    phases = "".join(
        f'<div class="mth-phase"><div class="mth-k">{_esc(name)}</div>'
        f'<div class="mth-v">{_esc(text)}</div></div>'
        for name, text in _METHODOLOGY_PHASES
    )
    # The lead is load-bearing and has to hold WITH a coverage checklist as well as without one (the
    # "no engagement-specific coverage checklist was recorded" note below only appears when there is
    # none). Without it, present-tense method prose still reads to a client as a description of what was
    # done on their engagement — which for a bulk-promoted report it is not.
    parts = [
        '<article class="mth">'
        '<p class="mth-lead">An assessment of this kind is conducted in the phases below, each feeding '
        "the next. This is a standing description of method, not a log of what was done on this "
        "engagement: what a given engagement covered is recorded on its coverage record and on the "
        "findings themselves.</p>"
        f'<div class="mth-phases">{phases}</div>'
    ]
    seen: set[str] = set()
    framing: list[str] = []
    for group in ctx.groups:
        slug = group.type_slug or ""
        if slug in seen or slug not in _METHODOLOGY_FRAMING:
            continue
        seen.add(slug)
        label, text = _METHODOLOGY_FRAMING[slug]
        framing.append(
            f'<div class="mth-frame"><h3>{_esc(label)}</h3><p>{_esc(text)}</p></div>'
        )
    if framing:
        parts.append(
            '<div class="mth-framing"><div class="mth-frame-cap">Framing by section type</div>'
            f'{"".join(framing)}</div>'
        )
    parts.append("</article>")
    return "".join(parts)


def _methodology_heading(ctx: ReportContext) -> str:
    """The Methodology section's heading — "Methodology and Coverage" when a coverage/reminder checklist
    opted into the report, else "Methodology". One function so the section and the table of contents cannot
    disagree about what the section is called (ext#43)."""
    coverage = [c for c in ctx.checklists if c.kind != "compliance"]
    return "Methodology and Coverage" if coverage else "Methodology"


_COMPLIANCE_HEADING = "Compliance Attestation"


def _render_methodology(ctx: ReportContext) -> str:
    """The Methodology section — ALWAYS non-empty, and always the owner of the ``#sec-methodology``
    anchor the toolbar links to.

    It always carries the standing description (:func:`_methodology_prose`). With coverage/reminder
    checklists that opted into the report it is titled "Methodology and Coverage" and renders them below
    the description as the coverage record; with none it is titled "Methodology" and adds an explicit note
    that no engagement-specific coverage record exists — so the standing text can never be misread as an
    attestation of coverage nobody recorded. Compliance checklists render as their own Compliance
    Attestation appendix, unchanged.
    """
    coverage = [c for c in ctx.checklists if c.kind != "compliance"]
    compliance = [c for c in ctx.checklists if c.kind == "compliance"]
    heading = _methodology_heading(ctx)
    body = _methodology_prose(ctx)
    if coverage:
        body += "".join(_render_checklist_coverage(c) for c in coverage)
    else:
        body += (
            '<p class="mth-note muted">No engagement-specific coverage checklist was recorded for this '
            "engagement; the description above is the standing methodology.</p>"
        )
    out = [
        f'<section class="sec group" id="sec-methodology"><h2 class="sec-h">{heading} '
        f'<span class="chev">▾</span></h2><div class="sec-body">{body}</div></section>'
    ]
    if compliance:
        attestation = "".join(_render_checklist_compliance(c) for c in compliance)
        # ``id="sec-compliance"`` so the table of contents can link it (ext#43). It is deliberately NOT a
        # template block key, so it gets no toolbar link — ``nav_keys`` only matches ``id="sec-<block key>"``.
        out.append(
            '<section class="sec group" id="sec-compliance">'
            f'<h2 class="sec-h">{_COMPLIANCE_HEADING} '
            f'<span class="chev">▾</span></h2><div class="sec-body">{attestation}</div></section>'
        )
    return "\n".join(out)


def _render_evidence_appendix(ctx: ReportContext, resolver: _AssetResolver) -> str:
    """Engagement-level evidence — ``ReportContext.artifacts``, i.e. artifacts attached to the engagement
    with no ``finding_id``.

    Renders nothing when there are none, which is the normal case and is why the toolbar's Evidence link
    is derived from the rendered anchor rather than hardcoded (see ``_render_document``). Before this
    section existed, such an upload was accepted, stored, answered ``201`` with a URL, and then appeared
    in no deliverable anywhere (ext#40) — the report is where that silence becomes visible again."""
    if not ctx.artifacts:
        return ""
    gallery = _render_artifact_gallery(ctx.artifacts, resolver, label=None)
    return (
        '<section class="sec group" id="sec-evidence">'
        '<h2 class="sec-h">Evidence <span class="chev">▾</span>'
        f'<span class="count">{len(ctx.artifacts)} '
        f'item{"s" if len(ctx.artifacts) != 1 else ""}</span></h2>'
        '<div class="sec-body"><p class="muted evidence-intro">Evidence recorded against this engagement '
        'as a whole rather than against one finding.</p>'
        f"{gallery}</div></section>"
    )


def _render_footer(ctx: ReportContext) -> str:
    generated = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    return f'<div class="foot">Generated by Scribble · engagement #{ctx.engagement_id} · {generated}</div>'


def _toc_entries(ctx: ReportContext, blocks: tuple[str, ...]) -> list[tuple[int, str, str, str]]:
    """``(level, anchor, label, severity)`` for every section this document will contain, in document order.

    Driven by the template's own ``blocks`` and by the same conditions the block renderers use, so the
    contents list what the document has and nothing else: the Evidence appendix appears only when there is
    engagement-level evidence, the Compliance Attestation only when a compliance checklist opted in, the
    Methodology heading is whatever ``_methodology_heading`` calls it, and a template that reorders or drops
    blocks reorders or drops their entries with them. ``severity`` is "" for anything but a finding.

    A group with no ``id`` cannot be linked, which is exactly the one case that produces no entry: the
    "No findings recorded for this engagement" placeholder ``_render_groups`` emits when ``ctx.groups`` is
    empty. ``tests/test_report_cover_and_toc.py`` pins the completeness of this list against the rendered
    document, so a future section that forgets to register here fails rather than going quietly missing.
    """
    entries: list[tuple[int, str, str, str]] = []
    for key in blocks:
        if key == "summary":
            entries.append((1, "sec-summary", "Executive Summary", ""))
        elif key == "findings":
            for group in ctx.groups:
                entries.append((1, _group_anchor(group), group.name, ""))
                for f in group.findings:
                    entries.append((2, f"finding-{f.id}", f.title, f.severity))
        elif key == "methodology":
            entries.append((1, "sec-methodology", _methodology_heading(ctx), ""))
            if any(c.kind == "compliance" for c in ctx.checklists):
                entries.append((1, "sec-compliance", _COMPLIANCE_HEADING, ""))
        elif key == "evidence":
            if ctx.artifacts:
                entries.append((1, "sec-evidence", "Evidence", ""))
    return entries


def _render_toc(ctx: ReportContext, blocks: tuple[str, ...]) -> str:
    """The PRINT-ONLY table of contents (ext#43): sections at level 1, each section's findings at level 2.

    No page numbers, deliberately and unavoidably: page numbers require paged-media counters
    (``target-counter()``), which Chrome's print engine does not implement — and Chrome is what renders the
    PDF here. The links are real anchors, so they are live in the PDF as well as on screen; what the printed
    contents give a reader is the document's shape and order.
    """
    entries = _toc_entries(ctx, blocks)
    if not entries:
        return ""
    items = []
    for level, anchor, label, severity in entries:
        sev = (
            f'<span class="toc-sev sev-{_esc(severity)}">{_esc(_SEV_LABELS.get(severity, severity))}</span>'
            if severity
            else ""
        )
        items.append(
            f'<li class="toc-l{level}"><a href="#{_esc(anchor)}">{_esc(label)}</a>{sev}</li>'
        )
    return (
        '<nav class="toc" id="sec-toc" aria-label="Contents">'
        '<div class="toc-cap">Contents</div>'
        f'<ol class="toc-list">{"".join(items)}</ol></nav>'
    )


def _render_block_by_key(
    key: str, ctx: ReportContext, resolver: _AssetResolver, *, blocks: tuple[str, ...] = ()
) -> str:
    """Render one top-level document block by its template key (see ``reporting.templates``). The filter
    bar travels with the ``findings`` block so it always sits directly above the finding groups, whatever
    order the template puts them in. ``blocks`` is the template's full block list — only the ``toc`` needs
    it, because a table of contents is a statement about the whole document."""
    if key == "cover":
        return _render_cover(ctx)
    if key == "toc":
        return _render_toc(ctx, blocks)
    if key == "summary":
        return _render_summary(ctx)
    if key == "findings":
        return (
            f"{_render_filter_bar(ctx)}\n"
            '<div id="sec-findings"></div>\n'
            f"{_render_groups(ctx, resolver)}"
        )
    if key == "methodology":
        return _render_methodology(ctx)
    if key == "evidence":
        return _render_evidence_appendix(ctx, resolver)
    return ""  # unknown key: templates.BLOCK_KEYS guards this; render nothing defensively


def _render_document(
    ctx: ReportContext,
    resolver: _AssetResolver,
    *,
    template: ReportTemplate,
    engagement_url: str | None = None,
    dashboard_url: str | None = None,
) -> str:
    # A template's theme forces the palette by stamping <html data-theme>; "auto" leaves it unstamped so
    # the report follows the viewer's prefers-color-scheme (the default behavior).
    theme_attr = f' data-theme="{template.theme}"' if template.theme in ("light", "dark") else ""
    # Render the blocks FIRST, then build the toolbar from what they produced: a section link is emitted
    # only for a block that actually put its ``id="sec-<key>"`` anchor in this document, in document
    # order. Deriving the nav from the rendered output (rather than from a fixed list, as it was until
    # ext#42) is what makes a dangling link structurally impossible — a block that renders nothing, or a
    # template that drops a block entirely, cannot leave a live link behind it.
    rendered = [
        (k, _render_block_by_key(k, ctx, resolver, blocks=template.blocks)) for k in template.blocks
    ]
    blocks = "\n".join(html for _k, html in rendered if html)
    nav_keys = tuple(k for k, html in rendered if f'id="sec-{k}"' in html)
    # ``has-cover`` lets the print sheet hide the masthead when — and only when — a cover page rendered in
    # its place (ext#43). It has to be a class the renderer stamps rather than a CSS ``:has()`` test,
    # because the masthead must keep printing for a template that carries no ``cover`` block: dropping it
    # unconditionally would leave such a PDF with no title anywhere.
    body_class = ' class="has-cover"' if any(k == "cover" and html for k, html in rendered) else ""
    header = _render_header(
        ctx, template, nav_keys=nav_keys, engagement_url=engagement_url, dashboard_url=dashboard_url
    )
    return (
        "<!doctype html>\n"
        f'<html lang="en"{theme_attr}>\n<head>\n<meta charset="utf-8"/>\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1"/>\n'
        f"<title>{_esc(ctx.engagement_name)} — Report</title>\n"
        f"<style>{_CSS}</style>\n</head>\n<body{body_class}>\n"
        f"{header}\n"
        '<main class="wrap">\n'
        f"{blocks}\n"
        f"{_render_footer(ctx)}\n"
        "</main>\n"
        f"<script>{_JS}</script>\n"
        "</body>\n</html>\n"
    )


def render_report_html(
    ctx: ReportContext,
    *,
    inline_assets: bool = False,
    artifact_bytes: ArtifactBytes | None = None,
    engagement_url: str | None = None,
    dashboard_url: str | None = None,
    template: str | None = None,
) -> str:
    """Render a full, self-contained HTML report for ``ctx``.

    ``inline_assets=True`` embeds evidence-gallery + inline-content images as ``data:`` URIs via the
    ``artifact_bytes(storage_path) -> bytes`` callback, so the returned document has zero external
    dependencies. With the default ``inline_assets=False`` (or a missing/failing ``artifact_bytes``),
    assets degrade gracefully to a "not embedded" placeholder rather than a broken reference.

    ``engagement_url``/``dashboard_url`` (both optional) render a small nav bar (``<nav
    class="report-nav no-print">``, hidden from print/PDF output) linking back to the engagement board
    and the Scribble dashboard -- the report -> app cross-links half of D3 nav (the app -> report
    direction lives in ``scribble/templates/scribble/engagement.html``'s ``View Report`` button).
    Callers with no request/app context (e.g. tests, standalone rendering) simply omit them and get the
    same header as before nav links existed.
    """
    resolver = _AssetResolver("inline" if inline_assets else "none", artifact_bytes)
    return _render_document(
        ctx, resolver, template=get_template(template),
        engagement_url=engagement_url, dashboard_url=dashboard_url,
    )


def export_zip(
    ctx: ReportContext, artifact_bytes: ArtifactBytes | None, *, template: str | None = None
) -> bytes:
    """Build a ZIP of ``report.html`` (assets externalized to ``artifacts/<name>``) + the referenced
    ``artifacts/`` files, for delivery without one giant inlined HTML file (PLAN.md §7)."""
    resolver = _AssetResolver("zip", artifact_bytes)
    html_doc = _render_document(ctx, resolver, template=get_template(template))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("report.html", html_doc)
        for name, storage_path in resolver.manifest.items():
            if not artifact_bytes:
                continue
            try:
                data = artifact_bytes(storage_path)
            except Exception:
                data = None
            if data:
                zf.writestr(f"artifacts/{name}", data)
    return buf.getvalue()


_CSS = """
/* Scribble report — modern light-first deliverable (Phase 1 redesign).
   Token-driven: light default + dark on-screen theme + print block. Every class hook the
   render functions emit is styled here, plus new widgets (topbar, masthead, sevbar, index). */
:root {
  --bg: #f6f8fa; --surface: #ffffff; --surface-2: #eff3f6;
  --ink: #131b24; --ink-2: #3d4b59; --muted: #6b7a89;
  --line: #e3e8ed; --line-2: #cbd4dd;
  --accent: #0f7a52; --accent-ink: #0a5b3d; --accent-wash: #e7f3ed;
  --sev-critical: #b3261e; --sev-high: #c2410c; --sev-medium: #a16207;
  --sev-low: #1d6fa5; --sev-info: #64748b;
  --radius: 8px; --measure: 72ch; --maxw: 1080px;
}
:root:not([data-theme="light"]) {
  @media (prefers-color-scheme: dark) {
    --bg: #0b1016; --surface: #131c25; --surface-2: #0e161e;
    --ink: #e7eef5; --ink-2: #b6c4d2; --muted: #8698a8;
    --line: #23303d; --line-2: #35485b;
    --accent: #2bb283; --accent-ink: #7ee0bc; --accent-wash: #0f2a20;
    --sev-critical: #ef5a4f; --sev-high: #ef8a44; --sev-medium: #d9a63e;
    --sev-low: #56a6da; --sev-info: #93a3b4;
  }
}
:root[data-theme="dark"] {
  --bg: #0b1016; --surface: #131c25; --surface-2: #0e161e;
  --ink: #e7eef5; --ink-2: #b6c4d2; --muted: #8698a8;
  --line: #23303d; --line-2: #35485b;
  --accent: #2bb283; --accent-ink: #7ee0bc; --accent-wash: #0f2a20;
  --sev-critical: #ef5a4f; --sev-high: #ef8a44; --sev-medium: #d9a63e;
  --sev-low: #56a6da; --sev-info: #93a3b4;
}

* { box-sizing: border-box; }
html { scroll-behavior: smooth; }
body {
  margin: 0; background: var(--bg); color: var(--ink);
  font-family: system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
  font-size: 15px; line-height: 1.6;
  -webkit-font-smoothing: antialiased; text-rendering: optimizeLegibility;
}
.wrap { max-width: var(--maxw); margin: 0 auto; padding: 0 28px; }
h1, h2, h3 { margin: 0 0 4px; text-wrap: balance; letter-spacing: -0.01em; }
a { color: var(--accent-ink); text-underline-offset: 2px; }
.muted { color: var(--muted); }
.mono {
  font-family: ui-monospace, "SF Mono", "JetBrains Mono", Menlo, Consolas, monospace;
  font-variant-numeric: tabular-nums;
}
.tnum { font-variant-numeric: tabular-nums; }

/* sticky command bar */
.topbar {
  position: sticky; top: 0; z-index: 50;
  background: color-mix(in srgb, var(--surface) 86%, transparent);
  backdrop-filter: saturate(1.4) blur(8px); border-bottom: 1px solid var(--line);
}
/* The bar now carries back-links AND section jumps AND the actions, which together can exceed the
   1080px measure — so it WRAPS instead of clipping. (It did clip: the back-to-engagement link and the
   Methodology jump were cut mid-word at a 1500px viewport once the back-links moved in here.) Each group is
   flex: 0 0 auto so a squeeze pushes a whole group to the next line rather than breaking a label. */
.topbar .wrap {
  display: flex; align-items: center; gap: 8px 16px; min-height: 52px; padding-top: 7px;
  padding-bottom: 7px; flex-wrap: wrap;
}
.tb-brand, .report-nav, .tb-sections, .tb-actions { flex: 0 0 auto; }
.tb-brand { font-weight: 700; font-size: 14px; display: flex; gap: 8px; align-items: center; }
.tb-mark { width: 9px; height: 18px; border-radius: 2px; background: var(--accent); }
.topbar nav { display: flex; gap: 2px; overflow-x: auto; }
.topbar nav a {
  font-size: 13px; font-weight: 550; color: var(--ink-2); text-decoration: none;
  padding: 6px 9px; border-radius: 6px; white-space: nowrap;
}
.topbar nav a:hover { background: var(--surface-2); color: var(--ink); }
.tb-actions { margin-left: auto; display: flex; gap: 8px; align-items: center; }
.tmpl-switch { display: inline-flex; align-items: center; gap: 6px; font-size: 12px; color: var(--muted); }
.tmpl-switch select {
  font-family: inherit; font-size: 12.5px; color: var(--ink); background: var(--surface);
  border: 1px solid var(--line-2); border-radius: 6px; padding: 5px 8px; cursor: pointer;
}
.btn {
  display: inline-flex; align-items: center; gap: 6px; font-family: inherit;
  font-size: 13px; font-weight: 650; cursor: pointer; border-radius: 7px;
  padding: 7px 13px; border: 1px solid transparent; white-space: nowrap;
}
.btn.primary { background: var(--accent); color: #fff; }
.btn.primary:hover { background: var(--accent-ink); }
.btn.ghost { background: transparent; color: var(--ink-2); border-color: var(--line-2); }
.btn.ghost:hover { border-color: var(--accent); color: var(--ink); }

/* masthead (flat, no gradient) */
.masthead { border-bottom: 1px solid var(--line); padding: 44px 0 30px; }
.masthead .row {
  display: flex; justify-content: space-between; align-items: flex-end; gap: 24px; flex-wrap: wrap;
}
/* Back-links: a toolbar slot (see _render_header / ext#45), ruled off from the section jumps beside
   them so "leave the report" and "jump within it" don't read as one undifferentiated row of links. */
.report-nav {
  display: flex; gap: 2px; align-items: center; font-size: 13px; font-weight: 600;
  padding-right: 10px; border-right: 1px solid var(--line);
}
.report-nav a { white-space: nowrap; }
.eyebrow {
  font-size: 12px; font-weight: 700; letter-spacing: .13em;
  text-transform: uppercase; color: var(--accent-ink);
}
.masthead h1 { font-size: 32px; line-height: 1.1; font-weight: 720; margin-top: 6px; }
.masthead .sub {
  color: var(--muted); font-size: 15px; margin-top: 8px;
  display: flex; gap: 8px 16px; flex-wrap: wrap;
}
.masthead .sub .dot { color: var(--line-2); }
.confidential {
  font-size: 11px; font-weight: 700; letter-spacing: .1em; text-transform: uppercase;
  color: var(--sev-critical);
  border: 1px solid color-mix(in srgb, var(--sev-critical) 40%, transparent);
  background: color-mix(in srgb, var(--sev-critical) 8%, transparent);
  border-radius: 5px; padding: 5px 11px; white-space: nowrap;
}
.toolbar { margin-top: 16px; display: flex; gap: 8px; flex-wrap: wrap; }

/* cover page + table of contents — PRINT ONLY (see reporting/templates.py). Hidden here in the base
   sheet rather than inside an `@media screen` block so they stay hidden under every non-print medium,
   and shown again only in the `@media print` block at the foot of this sheet. */
.cover, .toc { display: none; }
.cover-eyebrow {
  font-size: 12px; font-weight: 700; letter-spacing: .14em; text-transform: uppercase;
  color: var(--accent-ink);
}
.cover-title {
  font-size: 40pt; line-height: 1.08; font-weight: 720; letter-spacing: -0.02em; margin-top: 10px;
}
.cover-kind { margin-top: 10px; font-size: 15pt; color: var(--ink-2); }
/* Ruled with BORDERS, not with the container-background-through-a-1px-gap trick `.metrics` uses: a border
   is painted even with "Background graphics" off, so the cover's fact table needs no `print-color-adjust`
   entry to survive the print dialog's default (see the @media print note on meaningful fills). */
.cover-facts {
  margin: 0; display: grid; grid-template-columns: 1fr 1fr; column-gap: 24px;
  border-bottom: 1px solid var(--line);
}
.cover-fact { border-top: 1px solid var(--line); padding: 9px 2px; }
.cover-fact dt {
  font-size: 9pt; letter-spacing: .06em; text-transform: uppercase; color: var(--muted); font-weight: 700;
}
.cover-fact dd { margin: 3px 0 0; font-size: 12pt; font-weight: 600; word-break: break-word; }
.cover-foot { border-top: 2px solid var(--sev-critical); padding-top: 12px; }
.cover-badge {
  display: inline-block; font-size: 10pt; font-weight: 700; letter-spacing: .12em; text-transform: uppercase;
  color: var(--sev-critical); border: 1px solid var(--sev-critical); border-radius: 4px; padding: 4px 12px;
}
.cover-handling { margin: 10px 0 0; font-size: 10pt; color: var(--ink-2); max-width: 78ch; }
.toc-cap {
  font-size: 13px; font-weight: 700; letter-spacing: .06em; text-transform: uppercase;
  color: var(--ink-2); border-bottom: 1px solid var(--line); padding-bottom: 8px; margin-bottom: 10px;
}
.toc-list { list-style: none; margin: 0; padding: 0; }
.toc-list li { display: flex; align-items: baseline; gap: 10px; padding: 3px 0; }
.toc-list a { color: var(--ink); text-decoration: none; }
.toc-list li.toc-l1 { font-weight: 650; font-size: 12pt; margin-top: 8px; }
.toc-list li.toc-l2 { padding-left: 22px; font-size: 10.5pt; }
.toc-l2 a { color: var(--ink-2); }
.toc-sev {
  margin-left: auto; font-size: 8.5pt; font-weight: 700; letter-spacing: .04em; text-transform: uppercase;
}
.toc-sev.sev-critical { color: var(--sev-critical); }
.toc-sev.sev-high { color: var(--sev-high); }
.toc-sev.sev-medium { color: var(--sev-medium); }
.toc-sev.sev-low { color: var(--sev-low); }
.toc-sev.sev-info { color: var(--sev-info); }

/* sections */
main.wrap { padding-bottom: 72px; }
section.sec { padding-top: 34px; scroll-margin-top: 64px; }
.sec-h {
  font-size: 15px; font-weight: 700; letter-spacing: .04em; text-transform: uppercase;
  color: var(--ink-2); margin: 0 0 18px; padding-bottom: 10px; border-bottom: 1px solid var(--line);
  display: flex; align-items: center; gap: 10px; cursor: pointer; user-select: none;
}
.sec-h:hover { color: var(--ink); }
.sec-h .chev { font-size: 11px; color: var(--accent); transition: transform .15s ease; }
.sec-h .count {
  margin-left: auto; font-size: 13px; color: var(--muted);
  font-weight: 500; text-transform: none; letter-spacing: 0;
}
.sec.collapsed .sec-h { margin-bottom: 0; }
.sec.collapsed .sec-h .chev { transform: rotate(-90deg); }
.sec.collapsed .sec-body { display: none; }
.swatch { display: inline-block; width: 10px; height: 10px; border-radius: 3px; }

/* risk banner (verdict card) */
.risk {
  display: grid; grid-template-columns: auto 1fr; gap: 20px; align-items: center; margin: 4px 0 18px;
  background: var(--surface); border: 1px solid var(--line); border-left: 4px solid var(--sev-info);
  border-radius: var(--radius); padding: 18px 22px;
}
.risk .rating { text-align: center; padding-right: 20px; border-right: 1px solid var(--line); }
.risk .rating .label {
  font-size: 10px; letter-spacing: .12em; text-transform: uppercase; color: var(--muted);
}
.risk .rating .level {
  font-size: 25px; font-weight: 800; letter-spacing: -0.02em; line-height: 1.1; margin-top: 4px;
}
.risk .narr { color: var(--ink-2); font-size: 14.5px; line-height: 1.6; max-width: 66ch; }
.risk-critical { border-left-color: var(--sev-critical); }
.risk-critical .level { color: var(--sev-critical); }
.risk-high { border-left-color: var(--sev-high); }
.risk-high .level { color: var(--sev-high); }
.risk-medium { border-left-color: var(--sev-medium); }
.risk-medium .level { color: var(--sev-medium); }
.risk-low { border-left-color: var(--sev-low); }
.risk-low .level { color: var(--sev-low); }
.risk-info { border-left-color: var(--sev-info); }
.risk-info .level { color: var(--sev-info); }
.risk-clean { border-left-color: var(--accent); }
.risk-clean .level { color: var(--accent-ink); }

.summary-narrative {
  color: var(--ink); font-size: 15px; line-height: 1.6; margin: 2px 0 18px; max-width: var(--measure);
}

/* front matter at the head of the executive summary — see _render_front_matter. (Wording kept clear of
   the section headings themselves: this stylesheet ships inside the document, so a phrase repeated here
   is a false positive for anything grepping the rendered HTML for that prose.) */
.frontmatter { max-width: var(--measure); margin-bottom: 22px; }
.fm-block + .fm-block { margin-top: 18px; }
.fm-block h3 {
  font-size: 12px; letter-spacing: .07em; text-transform: uppercase;
  color: var(--accent-ink); font-weight: 700; margin-bottom: 6px;
}
.fm-lead { color: var(--ink); font-size: 15px; margin: 0; }
.fm-limits { margin: 0; padding-left: 20px; color: var(--ink-2); font-size: 14.5px; }
.fm-limits li { margin: 4px 0; }

/* what the severity ratings mean, under the bar that uses them */
.sev-defs { margin: 18px 0 8px; max-width: var(--measure); }
.sev-defs .cap {
  font-size: 12px; letter-spacing: .04em; text-transform: uppercase;
  color: var(--muted); font-weight: 600; margin-bottom: 8px;
}
.sd-row { display: flex; gap: 10px; align-items: baseline; padding: 4px 0; }
.sd-row .sev-tag { flex: 0 0 auto; min-width: 78px; justify-content: center; }
.sd-text { color: var(--ink-2); font-size: 14px; }

/* severity distribution bar (the one chart) */
.sevbar-wrap { margin: 20px 0 8px; }
.sevbar-head {
  display: flex; justify-content: space-between; align-items: baseline; margin-bottom: 8px;
}
.sevbar-head .t {
  font-size: 12px; letter-spacing: .04em; text-transform: uppercase; color: var(--muted); font-weight: 600;
}
.sevbar-head .total { font-size: 13px; color: var(--ink-2); }
.sevbar-head .total b { font-weight: 800; font-size: 15px; }
.sevbar {
  display: flex; height: 32px; border-radius: 6px; overflow: hidden; border: 1px solid var(--line);
}
.sevbar .seg {
  display: flex; align-items: center; justify-content: center; min-width: 28px;
  color: #fff; font-size: 12.5px; font-weight: 700; font-variant-numeric: tabular-nums;
}
.sevbar .seg.sev-critical { background: var(--sev-critical); }
.sevbar .seg.sev-high { background: var(--sev-high); }
.sevbar .seg.sev-medium { background: var(--sev-medium); }
.sevbar .seg.sev-low { background: var(--sev-low); }
.sevbar .seg.sev-info { background: var(--sev-info); }
.sevlegend { display: flex; gap: 8px 18px; flex-wrap: wrap; margin-top: 10px; }
.sevlegend .item { display: flex; align-items: center; gap: 7px; font-size: 12.5px; color: var(--ink-2); }
.sevlegend .sw { width: 10px; height: 10px; border-radius: 2px; }
.sevlegend b { font-variant-numeric: tabular-nums; }

/* metrics row */
.metrics {
  display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 1px;
  background: var(--line); border: 1px solid var(--line); border-radius: var(--radius);
  overflow: hidden; margin-top: 6px;
}
.metric { background: var(--surface); padding: 12px 15px; }
.metric .k { font-size: 11px; letter-spacing: .05em; text-transform: uppercase; color: var(--muted); }
.metric .v {
  font-size: 16px; font-weight: 700; margin-top: 3px; letter-spacing: -0.01em; word-break: break-word;
}

/* findings-at-a-glance index */
.index-wrap { margin-top: 24px; }
.index-wrap .cap {
  font-size: 12px; letter-spacing: .04em; text-transform: uppercase;
  color: var(--muted); font-weight: 600; margin-bottom: 8px;
}
table.index { width: 100%; border-collapse: collapse; font-size: 14px; }
table.index thead th {
  text-align: left; font-size: 11px; letter-spacing: .05em; text-transform: uppercase;
  color: var(--muted); font-weight: 600; padding: 7px 10px; border-bottom: 1px solid var(--line-2);
}
table.index tbody td { padding: 9px 10px; border-bottom: 1px solid var(--line); vertical-align: middle; }
table.index tbody tr:hover { background: var(--surface-2); }
table.index td.ix-sev { width: 96px; white-space: nowrap; }
table.index td.ix-title a { color: var(--ink); text-decoration: none; font-weight: 550; }
table.index td.ix-title a:hover { color: var(--accent-ink); text-decoration: underline; }
table.index td.ix-host { color: var(--muted); white-space: nowrap; }
table.index td.ix-cvss {
  text-align: right; white-space: nowrap; font-weight: 650; font-variant-numeric: tabular-nums;
}

/* severity tag + badge (tinted, not saturated pill) */
.sev-tag, .sev-badge {
  display: inline-flex; align-items: center; gap: 6px; font-size: 11.5px; font-weight: 700;
  letter-spacing: .02em; padding: 2px 9px; border-radius: 5px; border: 1px solid transparent;
  text-transform: uppercase;
}
.sev-tag.sev-critical, .sev-badge.sev-critical {
  color: var(--sev-critical);
  background: color-mix(in srgb, var(--sev-critical) 11%, transparent);
  border-color: color-mix(in srgb, var(--sev-critical) 26%, transparent);
}
.sev-tag.sev-high, .sev-badge.sev-high {
  color: var(--sev-high);
  background: color-mix(in srgb, var(--sev-high) 12%, transparent);
  border-color: color-mix(in srgb, var(--sev-high) 26%, transparent);
}
.sev-tag.sev-medium, .sev-badge.sev-medium {
  color: var(--sev-medium);
  background: color-mix(in srgb, var(--sev-medium) 13%, transparent);
  border-color: color-mix(in srgb, var(--sev-medium) 28%, transparent);
}
.sev-tag.sev-low, .sev-badge.sev-low {
  color: var(--sev-low);
  background: color-mix(in srgb, var(--sev-low) 12%, transparent);
  border-color: color-mix(in srgb, var(--sev-low) 26%, transparent);
}
.sev-tag.sev-info, .sev-badge.sev-info {
  color: var(--sev-info);
  background: color-mix(in srgb, var(--sev-info) 13%, transparent);
  border-color: color-mix(in srgb, var(--sev-info) 28%, transparent);
}

/* filters */
.filters { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; margin: 8px 0 18px; }
.filters input[type=search] {
  background: var(--surface); border: 1px solid var(--line-2); border-radius: 7px; color: var(--ink);
  padding: 8px 11px; font-size: 13px; min-width: 240px; flex: 1 1 240px; font-family: inherit;
}
.filters input[type=search]::placeholder { color: var(--muted); }
.chip-toggle {
  cursor: pointer; user-select: none; border: 1px solid var(--line-2); border-radius: 6px;
  padding: 5px 11px; font-size: 11.5px; font-weight: 700; letter-spacing: .02em;
  text-transform: uppercase; color: var(--muted); opacity: .55;
}
.chip-toggle.on { opacity: 1; }
.chip-toggle.sev-critical.on {
  color: var(--sev-critical); border-color: color-mix(in srgb, var(--sev-critical) 45%, transparent);
}
.chip-toggle.sev-high.on {
  color: var(--sev-high); border-color: color-mix(in srgb, var(--sev-high) 45%, transparent);
}
.chip-toggle.sev-medium.on {
  color: var(--sev-medium); border-color: color-mix(in srgb, var(--sev-medium) 45%, transparent);
}
.chip-toggle.sev-low.on {
  color: var(--sev-low); border-color: color-mix(in srgb, var(--sev-low) 45%, transparent);
}
.chip-toggle.sev-info.on {
  color: var(--sev-info); border-color: color-mix(in srgb, var(--sev-info) 45%, transparent);
}
.sort-toggle { font-size: 12px; color: var(--muted); display: flex; align-items: center; gap: 5px; }
.filter-count { font-size: 12.5px; color: var(--muted); margin-left: auto; }

/* finding cards */
.findings-list { display: flex; flex-direction: column; gap: 14px; }
.finding {
  background: var(--surface); border: 1px solid var(--line); border-left: 3px solid var(--line-2);
  border-radius: var(--radius); padding: 16px 18px 18px;
}
.finding.sev-critical { border-left-color: var(--sev-critical); }
.finding.sev-high { border-left-color: var(--sev-high); }
.finding.sev-medium { border-left-color: var(--sev-medium); }
.finding.sev-low { border-left-color: var(--sev-low); }
.finding.sev-info { border-left-color: var(--sev-info); }
.finding.filtered-out { display: none; }
.finding-head {
  display: flex; justify-content: space-between; align-items: flex-start; gap: 12px; flex-wrap: wrap;
}
.finding h3 { font-size: 16px; font-weight: 680; line-height: 1.3; }
.finding-badges { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
.finding-meta { display: flex; flex-wrap: wrap; gap: 6px 8px; margin: 11px 0 4px; }
.chip {
  display: inline-flex; align-items: center; gap: 5px; font-size: 12px; color: var(--ink-2);
  background: var(--surface-2); border: 1px solid var(--line); border-radius: 5px; padding: 3px 9px;
}
.chip.cvss { color: var(--ink); font-weight: 650; font-variant-numeric: tabular-nums; }
.finding-body { max-width: var(--measure); }
.finding-body .block { margin-top: 14px; }
.finding-body .block-label {
  font-size: 11px; letter-spacing: .07em; text-transform: uppercase;
  color: var(--accent-ink); font-weight: 700; margin-bottom: 5px;
}
.finding-body .block-body { color: var(--ink-2); font-size: 14.5px; }
.finding-body .block-body p { margin: 4px 0; }
.finding-body .block-body img { max-width: 100%; border-radius: 6px; }
.finding-body .block-body pre {
  background: var(--surface-2); border: 1px solid var(--line); border-radius: 6px;
  padding: 10px 12px; font-size: 12.5px; overflow: auto; max-height: 24em;
}
.finding-body .block-body code {
  font-family: ui-monospace, "SF Mono", Menlo, Consolas, monospace; font-size: 13px;
}
.empty { color: var(--muted); font-style: italic; }
.finding-body .asset-list { margin: 4px 0 0; padding-left: 18px; }
.finding-body .asset-list li {
  margin: 3px 0; font-family: ui-monospace, "SF Mono", Menlo, Consolas, monospace;
  font-size: 13px; font-variant-numeric: tabular-nums;
}
.finding-body .block.recommendations .block-label { color: var(--accent-ink); }

/* children (affected hosts) */
.children {
  margin-top: 14px; border: 1px solid var(--line); border-radius: 7px;
  background: var(--surface-2); overflow: hidden;
}
.children summary {
  cursor: pointer; user-select: none; padding: 9px 13px; font-size: 12px; font-weight: 700;
  letter-spacing: .03em; text-transform: uppercase; color: var(--ink-2);
}
.children summary:hover { color: var(--ink); }
.children-table { width: 100%; border-collapse: collapse; font-size: 13.5px; }
.children-table th, .children-table td {
  padding: 7px 13px; border-top: 1px solid var(--line); text-align: left; vertical-align: top;
}
.children-table th {
  color: var(--muted); font-weight: 600; font-size: 11px; text-transform: uppercase; letter-spacing: .04em;
}
.children-table td.child-host {
  white-space: nowrap; font-weight: 600; font-family: ui-monospace, Menlo, monospace;
}
.children-table td.child-evidence { color: var(--muted); }
.children-table td.child-evidence .child-facts { margin-bottom: 6px; }
/* A child instance's evidence gallery lives inside a table cell — tighter grid + shorter thumbs than the
   full-width per-finding gallery, so a per-host screenshot fits the row it belongs to. */
.children-table td.child-evidence .evidence { margin-top: 0; }
.children-table td.child-evidence .evidence-grid {
  grid-template-columns: repeat(auto-fill, minmax(120px, 1fr)); gap: 8px;
}
.children-table td.child-evidence .evidence-item img { height: 92px; }

/* evidence gallery */
.evidence { margin-top: 16px; }
.evidence-label {
  font-size: 11px; letter-spacing: .06em; text-transform: uppercase;
  color: var(--muted); font-weight: 600; margin-bottom: 8px;
}
.evidence-grid { display: grid; gap: 12px; grid-template-columns: repeat(auto-fill, minmax(180px, 1fr)); }
.evidence-item {
  border: 1px solid var(--line); border-radius: 7px; overflow: hidden;
  background: var(--surface-2); margin: 0;
}
.evidence-item img {
  display: block; width: 100%; height: 128px; object-fit: cover; background: var(--surface-2);
}
.evidence-item figcaption, .evidence-item .cap {
  padding: 7px 9px; font-size: 11.5px; color: var(--muted); word-break: break-word;
}
.evidence-item.file { padding: 10px; font-size: 13px; }
.evidence-item.missing { padding: 10px; font-size: 13px; color: var(--muted); }
.evidence-link:hover { opacity: .85; }
.file-chip { color: var(--ink); text-decoration: none; }
.lightbox { display: none; }
.lightbox:target {
  display: flex; position: fixed; inset: 0; z-index: 1000; align-items: center; justify-content: center;
  padding: 24px; background: rgba(3, 8, 15, .92); cursor: zoom-out;
}
.lightbox img {
  max-width: 100%; max-height: 100%; object-fit: contain; box-shadow: 0 8px 40px rgba(0, 0, 0, .6);
}

/* methodology (the standing description; see _render_methodology) */
.mth { max-width: var(--measure); }
.mth-lead { color: var(--ink-2); font-size: 14.5px; margin: 0 0 14px; }
.mth-phases {
  display: grid; gap: 1px; background: var(--line); border: 1px solid var(--line);
  border-radius: var(--radius); overflow: hidden;
}
.mth-phase { background: var(--surface); padding: 11px 15px; }
.mth-k {
  font-size: 11px; letter-spacing: .05em; text-transform: uppercase;
  color: var(--accent-ink); font-weight: 700;
}
.mth-v { color: var(--ink-2); font-size: 14px; margin-top: 3px; }
.mth-framing { margin-top: 20px; }
.mth-frame-cap {
  font-size: 12px; letter-spacing: .04em; text-transform: uppercase;
  color: var(--muted); font-weight: 600; margin-bottom: 8px;
}
.mth-frame { margin-bottom: 14px; }
.mth-frame h3 { font-size: 14.5px; font-weight: 680; }
.mth-frame p { color: var(--ink-2); font-size: 14px; margin: 4px 0 0; }
.mth-note { font-size: 13px; margin-top: 16px; font-style: italic; }

/* engagement-level evidence appendix */
.evidence-intro { font-size: 14px; margin: 0 0 12px; }

/* checklists */
.ck-list {
  background: var(--surface); border: 1px solid var(--line); border-radius: var(--radius);
  padding: 14px 18px; margin: 12px 0;
}
.ck-head { display: flex; justify-content: space-between; align-items: baseline; gap: 12px; flex-wrap: wrap; }
.ck-head h3 { font-size: 15px; font-weight: 680; }
.ck-rollup { display: flex; gap: 7px; flex-wrap: wrap; }
.ck-chip {
  font-size: 11px; font-weight: 650; padding: 2px 9px; border-radius: 5px;
  border: 1px solid var(--line-2); color: var(--muted);
}
.ck-section {
  font-size: 11px; text-transform: uppercase; letter-spacing: .05em; color: var(--muted);
  margin: 12px 0 4px; border-bottom: 1px solid var(--line); padding-bottom: 3px;
}
.ck-item {
  display: flex; gap: 12px; align-items: flex-start; padding: 8px 0; border-top: 1px solid var(--line);
}
.ck-item:first-of-type { border-top: none; }
.ck-item-body { flex: 1; }
.ck-item-text { font-size: 14px; }
.ck-note { font-size: 12.5px; color: var(--muted); margin-top: 2px; }
.ck-finding { font-size: 12px; }
.ck-badge {
  display: inline-block; min-width: 76px; text-align: center; padding: 3px 8px; border-radius: 5px;
  font-size: 10.5px; font-weight: 700; letter-spacing: .03em; text-transform: uppercase;
  color: #fff; flex: 0 0 auto;
}
.ck-satisfied { background: var(--accent-ink); }
.ck-deficient { background: var(--sev-high); }
.ck-not_applicable { background: var(--line-2); color: var(--ink); }
.ck-open { background: var(--sev-low); }
.ck-chip.ck-satisfied {
  color: var(--accent-ink); background: transparent;
  border-color: color-mix(in srgb, var(--accent) 40%, transparent);
}
.ck-chip.ck-deficient {
  color: var(--sev-high); background: transparent;
  border-color: color-mix(in srgb, var(--sev-high) 40%, transparent);
}
.ck-chip.ck-not_applicable { border-color: var(--line-2); background: transparent; }
.ck-chip.ck-open {
  color: var(--sev-low); background: transparent;
  border-color: color-mix(in srgb, var(--sev-low) 40%, transparent);
}
.ck-tablewrap { overflow-x: auto; }
.ck-table { width: 100%; border-collapse: collapse; font-size: 13px; margin: 4px 0 10px; }
.ck-table th, .ck-table td {
  padding: 6px 10px; border-top: 1px solid var(--line); text-align: left; vertical-align: top;
}
.ck-table th { color: var(--muted); font-size: 11px; text-transform: uppercase; font-weight: 600; }
.ck-table td.ck-ctrl { white-space: nowrap; font-weight: 600; font-variant-numeric: tabular-nums; }

.foot {
  margin-top: 44px; padding-top: 14px; border-top: 1px solid var(--line);
  font-size: 12.5px; color: var(--muted);
}

@media (max-width: 760px) {
  .wrap { padding: 0 16px; }
  /* Narrow screens drop the in-document section jumps, but KEEP the back-links (.report-nav) — they
     are the only way out of the report, and the toolbar has room for two. */
  .topbar .tb-sections { display: none; }
  .report-nav { border-right: none; padding-right: 0; }
  .masthead { padding: 30px 0 22px; }
  .masthead h1 { font-size: 25px; }
  .masthead .row { align-items: flex-start; }
  section.sec { padding-top: 26px; }
  .risk { grid-template-columns: 1fr; }
  .risk .rating {
    border-right: none; border-bottom: 1px solid var(--line); padding: 0 0 14px; text-align: left;
  }
  table.index td.ix-host { display: none; }
  .btn { padding: 7px 10px; font-size: 12.5px; }
}
@media (max-width: 520px) { .topbar [data-action="collapse-all"] { display: none; } }
@media (max-width: 460px) {
  .masthead h1 { font-size: 22px; }
  .metrics { grid-template-columns: 1fr 1fr; }
}
@media (prefers-reduced-motion: reduce) { * { transition: none !important; scroll-behavior: auto; } }
:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; border-radius: 3px; }

@media print {
  .lightbox, .lightbox:target { display: none !important; }
  /* SPECIFICITY IS LOAD-BEARING HERE, and getting it wrong is invisible on screen. The dark palette is
     declared on `:root:not([data-theme="light"])` and `:root[data-theme="dark"]` — both 0-2-0 — so a
     plain `:root` print override (0-1-0) LOSES to them no matter that it comes later in the sheet. That
     is measurable: printing from a dark-mode browser computed `body { color: rgb(231,238,245) }` (the
     dark near-white ink) on unpainted white paper. The two selectors below are 0-2-0 and between them
     match every case — no stamp, `data-theme="light"`, `data-theme="dark"` — so paper always gets the
     paper palette. A template that forces the dark theme still prints light: the sheet is white.
     `--sev-*` is pinned here for the same reason (ext#39's second half): it was the one family the print
     block never overrode at all, so a dark-mode viewer printed the dark severity ramp. */
  :root:not([data-theme="dark"]), :root[data-theme="dark"] {
    --bg: #fff; --surface: #fff; --surface-2: #f4f6f8; --ink: #10202e; --ink-2: #33424f;
    --muted: #5a6b7b; --line: #dce2e8; --line-2: #c3ccd6;
    --sev-critical: #b3261e; --sev-high: #c2410c; --sev-medium: #a16207;
    --sev-low: #1d6fa5; --sev-info: #64748b;
  }
  /* Colour that CARRIES MEANING has to survive "Background graphics: off" — the Chrome print-dialog
     DEFAULT, and what a client actually gets. An element left at the permissive `economy` default may
     have its background fill dropped while its text colour is still painted, which is exactly how the
     severity bar printed as an empty outline with #fff numerals on white paper and the legend swatches
     vanished, leaving bare columns of digits. Scoped ON PURPOSE: a blanket `* { print-color-adjust:
     exact }` would also force the page's own --bg onto the paper.
     `.metrics` and `.metric` must be listed TOGETHER: the tile rules are the container's background
     showing through a 1px grid gap, so keeping one background and dropping the other prints either no
     rules at all or one solid grey block.
     `.chip-toggle` is moot while `.filters` is display:none below — kept so the rule doesn't rot if the
     filter chips are ever printed. */
  .sevbar, .sevbar .seg, .sevlegend .sw,
  .sev-tag, .sev-badge, .ck-badge, .chip-toggle,
  .metrics, .metric, .mth-phases, .mth-phase {
    -webkit-print-color-adjust: exact; print-color-adjust: exact;
  }
  body { font-size: 10.5pt; }
  .topbar, .filters, .sec-h .chev, .no-print, .toolbar, .report-nav { display: none !important; }
  /* The printed deliverable opens on its title page, then its contents (ext#43). Both blocks are
     `display: none` in the base sheet and exist ONLY here.
     `body.has-cover .masthead` is not just de-duplication: header.masthead sits BEFORE main.wrap in the
     document, so leaving it visible prints it ahead of the cover and page 1 is still not the title page.
     (Do not write markup tags in these comments: the stylesheet ships inside the document, so a literal
     tag here is matched by anything grepping the rendered HTML — it broke a masthead test once.)
     The class is stamped by the renderer only when a cover actually rendered, so a template without a
     `cover` block keeps its masthead and still has a title on paper. */
  .cover {
    display: flex; flex-direction: column; justify-content: space-between; gap: 24px;
    min-height: 88vh; break-after: page; padding-top: 12px;
  }
  .toc { display: block; break-after: page; padding-top: 12px; }
  .toc-list li { break-inside: avoid; }
  body.has-cover .masthead { display: none !important; }
  .sec.collapsed .sec-body { display: block !important; }
  .finding, .metric, .risk, .evidence-item, .ck-item, .ck-table tr, .index-wrap { break-inside: avoid; }
  .mth-phase, .mth-frame { break-inside: avoid; }
  .children-table { break-inside: avoid; }
  .sec-h { break-after: avoid; }
  .finding-body .block-body pre { max-height: none; overflow: visible; }
  a { color: #10202e; text-decoration: underline; }
}
"""

_JS = """
(function () {
  "use strict";
  // Layout switcher: reload with ?template=<name> so the server re-renders in the chosen template.
  var tmpl = document.getElementById("template-select");
  if (tmpl) {
    tmpl.addEventListener("change", function () {
      var u = new URL(window.location.href);
      u.searchParams.set("template", tmpl.value);
      window.location.assign(u.toString());
    });
  }
  var sections = Array.prototype.slice.call(document.querySelectorAll("section.sec"));
  sections.forEach(function (sec) {
    var h = sec.querySelector(".sec-h");
    if (!h) return;
    h.addEventListener("click", function () { sec.classList.toggle("collapsed"); });
  });
  function setAllCollapsed(collapsed) {
    sections.forEach(function (sec) { sec.classList.toggle("collapsed", collapsed); });
  }
  function openAllChildren() {
    document.querySelectorAll("details.children").forEach(function (d) { d.open = true; });
  }
  document.querySelectorAll("[data-action]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var a = btn.getAttribute("data-action");
      if (a === "print") { setAllCollapsed(false); openAllChildren(); window.print(); }
      else if (a === "expand-all") { setAllCollapsed(false); }
      else if (a === "collapse-all") { setAllCollapsed(true); }
    });
  });

  var cards = Array.prototype.slice.call(document.querySelectorAll(".finding"));
  var chips = Array.prototype.slice.call(document.querySelectorAll("#finding-filters .chip-toggle"));
  var search = document.getElementById("finding-search");
  var countEl = document.getElementById("finding-count");
  function activeSevs() {
    var s = {};
    chips.forEach(function (c) { if (c.classList.contains("on")) s[c.getAttribute("data-sev")] = true; });
    return s;
  }
  function applyFilter() {
    var sevs = activeSevs();
    var q = (search && search.value.trim().toLowerCase()) || "";
    var shown = 0;
    cards.forEach(function (card) {
      var sevOk = sevs[card.getAttribute("data-sev")];
      var textOk = !q || (card.textContent || "").toLowerCase().indexOf(q) !== -1;
      var hit = sevOk && textOk;
      card.classList.toggle("filtered-out", !hit);
      if (hit) shown++;
    });
    if (countEl) countEl.textContent = "showing " + shown + " of " + cards.length;
  }
  chips.forEach(function (c) {
    function toggle() { c.classList.toggle("on"); applyFilter(); }
    c.addEventListener("click", toggle);
    c.addEventListener("keydown", function (e) {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggle(); }
    });
  });
  if (search) search.addEventListener("input", applyFilter);
  applyFilter();

  // Severity sort is a non-destructive on-screen view toggle; document/print order always reverts
  // to board order (the canonical order == the order the server rendered the cards in).
  var lists = Array.prototype.slice.call(document.querySelectorAll(".findings-list"));
  var originalOrder = lists.map(function (list) { return Array.prototype.slice.call(list.children); });
  var sortBox = document.getElementById("sort-severity");
  function applySort() {
    var bySeverity = sortBox && sortBox.checked;
    lists.forEach(function (list, i) {
      var children = bySeverity
        ? originalOrder[i].slice().sort(function (a, b) {
            return (+a.dataset.sevrank) - (+b.dataset.sevrank);
          })
        : originalOrder[i];
      children.forEach(function (el) { list.appendChild(el); });
    });
  }
  function resetSort() {
    if (sortBox) sortBox.checked = false;
    applySort();
  }
  if (sortBox) sortBox.addEventListener("change", applySort);

  window.addEventListener("beforeprint", function () {
    setAllCollapsed(false); openAllChildren(); resetSort();
  });
})();
"""
