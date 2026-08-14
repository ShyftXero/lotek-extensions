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

ArtifactBytes = Callable[[str], "bytes | None"]

SEVERITY_ORDER: tuple[str, ...] = tuple(s.value for s in _ENUM_SEVERITY_ORDER)  # worst-first

_BLOCK_LABELS = {"description": "Description", "remediation": "Remediation", "details": "Details"}
_BLOCK_ORDER = ("description", "remediation", "details")

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
    if not f.artifacts:
        return ""
    items = "".join(_render_gallery_item(a, resolver) for a in f.artifacts)
    n = len(f.artifacts)
    return (
        '<div class="evidence"><div class="evidence-label">'
        f'Evidence ({n})</div><div class="evidence-grid">{items}</div></div>'
    )


def _target_chips(f: FindingCtx) -> str:
    chips: list[str] = []
    if f.target_host:
        label = f.target_host + (f":{f.target_port}" if f.target_port else "")
        chips.append(f'<span class="chip">Host: {_esc(label)}</span>')
    if f.target_url:
        href = _safe_href(f.target_url)
        if href:
            chips.append(f'<span class="chip">URL: <a href="{_esc(href)}">{_esc(f.target_url)}</a></span>')
        else:
            chips.append(f'<span class="chip">URL: {_esc(f.target_url)}</span>')
    return "".join(chips)


def _render_blocks(f: FindingCtx, resolver: _AssetResolver) -> str:
    parts: list[str] = []
    seen: set[str] = set()
    for key in _BLOCK_ORDER:
        seen.add(key)
        fragment = f.blocks_html.get(key)
        if fragment:
            parts.append(_render_block(key, fragment, resolver))
    for key, fragment in f.blocks_html.items():
        if key in seen or not fragment:
            continue
        parts.append(_render_block(key, fragment, resolver))
    return "\n".join(parts) or '<p class="empty">No content.</p>'


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


def _render_children(f: FindingCtx) -> str:
    """A COMPACT per-host list for a parent finding's children -- rendered once, collapsed by default,
    instead of one full finding card per instance (see module docstring header)."""
    if not f.children:
        return ""
    rows = "".join(
        f'<tr><td class="child-host">{_esc(_child_host_label(c))}</td>'
        f'<td class="child-evidence">{_esc(_child_summary_text(c))}</td></tr>'
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
    meta_chips = _target_chips(f)
    meta_html = f'<div class="finding-meta">{meta_chips}</div>' if meta_chips else ""
    return (
        f'<article class="finding sev-{_esc(sev)}" data-sev="{_esc(sev)}" data-sevrank="{rank}" '
        f'id="finding-{f.id}">'
        f'<div class="finding-head"><h3>{_esc(f.title)}</h3>'
        f'<div class="finding-badges">{badges}</div></div>'
        f"{meta_html}"
        f'<div class="finding-body">{_render_blocks(f, resolver)}</div>'
        f"{_render_gallery(f, resolver)}"
        f"{_render_children(f)}"
        "</article>"
    )


def _render_group(group: GroupCtx, resolver: _AssetResolver) -> str:
    gid = group.id if group.id is not None else "ungrouped"
    swatch = f'<span class="swatch" style="background:{_esc(group.color)}"></span>' if group.color else ""
    n = len(group.findings)
    findings_html = "".join(_render_finding(f, resolver) for f in group.findings) or (
        '<p class="empty">No findings in this section.</p>'
    )
    return (
        f'<section class="sec group" data-group-id="{_esc(str(gid))}">'
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
    ctx: ReportContext, *, engagement_url: str | None = None, dashboard_url: str | None = None
) -> str:
    """A sticky command bar (section jumps + print/expand/collapse) over a flat, hairline-ruled
    masthead. All on-screen chrome carries ``no-print`` so the PDF opens straight on the masthead."""
    eyebrow = _esc(ctx.client_name or ctx.company_name or "Security Assessment")
    subtitle = f"{_esc(ctx.scope_type)} assessment" if ctx.scope_type else "Penetration test report"
    sub_bits = [f"<span>{subtitle}</span>"]
    if ctx.start_date or ctx.end_date:
        sub_bits.append('<span class="dot">·</span>')
        sub_bits.append(f'<span>{_esc(ctx.start_date or "?")} – {_esc(ctx.end_date or "present")}</span>')
    assessor = ctx.variables.get("ASSESSOR", "") if ctx.variables else ""
    if assessor:
        sub_bits.append('<span class="dot">·</span>')
        sub_bits.append(f"<span>Assessor: {_esc(assessor)}</span>")

    back_links: list[str] = []
    if dashboard_url:
        back_links.append(f'<a href="{_esc(dashboard_url)}">← Dashboard</a>')
    if engagement_url:
        back_links.append(f'<a href="{_esc(engagement_url)}">← Back to engagement</a>')
    back_html = f'<nav class="report-nav no-print">{"".join(back_links)}</nav>' if back_links else ""

    topbar = (
        '<div class="topbar no-print"><div class="wrap">'
        '<div class="tb-brand"><span class="tb-mark"></span> Scribble</div>'
        '<nav><a href="#sec-summary">Summary</a><a href="#sec-findings">Findings</a>'
        '<a href="#sec-methodology">Methodology</a></nav>'
        '<div class="tb-actions">'
        '<button class="btn ghost" type="button" data-action="expand-all">Expand all</button>'
        '<button class="btn ghost" type="button" data-action="collapse-all">Collapse all</button>'
        '<button class="btn primary" type="button" data-action="print">⎙ Print / PDF</button>'
        "</div></div></div>"
    )
    masthead = (
        '<header class="masthead"><div class="wrap">'
        f"{back_html}"
        '<div class="row"><div>'
        f'<div class="eyebrow">{eyebrow}</div><h1>{_esc(ctx.engagement_name)}</h1>'
        f'<div class="sub">{"".join(sub_bits)}</div></div>'
        '<span class="confidential">Confidential</span></div></div></header>'
    )
    return topbar + masthead


_SEV_LABELS = {"critical": "Critical", "high": "High", "medium": "Medium", "low": "Low", "info": "Info"}


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
    narrative_html = f'<p class="summary-narrative">{_esc(ctx.narrative)}</p>' if ctx.narrative else ""
    return (
        '<section class="sec" id="sec-summary">'
        '<h2 class="sec-h">Executive Summary <span class="chev">▾</span></h2>'
        '<div class="sec-body">'
        f'<div class="risk {banner_class}">'
        '<div class="rating"><div class="label">Overall Risk</div>'
        f'<div class="level">{_esc(banner_label)}</div></div>'
        f'<div class="narr">{_esc(banner_sub)}</div></div>'
        f"{narrative_html}"
        f"{_sev_bar(rollup)}"
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


def _render_checklists(ctx: ReportContext) -> str:
    """Coverage/reminder checklists render a Methodology and Coverage section; compliance checklists
    render a Compliance Attestation appendix grouped by framework. Only checklists that opted into the
    report (``include_in_report``) reach here -- see ``build_report_context._build_checklists``."""
    if not ctx.checklists:
        return ""
    coverage = [c for c in ctx.checklists if c.kind != "compliance"]
    compliance = [c for c in ctx.checklists if c.kind == "compliance"]
    out: list[str] = []
    if coverage:
        body = "".join(_render_checklist_coverage(c) for c in coverage)
        out.append(
            '<section class="sec group"><h2 class="sec-h">Methodology and Coverage '
            f'<span class="chev">▾</span></h2><div class="sec-body">{body}</div></section>'
        )
    if compliance:
        body = "".join(_render_checklist_compliance(c) for c in compliance)
        out.append(
            '<section class="sec group"><h2 class="sec-h">Compliance Attestation '
            f'<span class="chev">▾</span></h2><div class="sec-body">{body}</div></section>'
        )
    return "\n".join(out)


def _render_footer(ctx: ReportContext) -> str:
    generated = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    return f'<div class="foot">Generated by Scribble · engagement #{ctx.engagement_id} · {generated}</div>'


def _render_document(
    ctx: ReportContext,
    resolver: _AssetResolver,
    *,
    engagement_url: str | None = None,
    dashboard_url: str | None = None,
) -> str:
    return (
        "<!doctype html>\n"
        '<html lang="en">\n<head>\n<meta charset="utf-8"/>\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1"/>\n'
        f"<title>{_esc(ctx.engagement_name)} — Report</title>\n"
        f"<style>{_CSS}</style>\n</head>\n<body>\n"
        f"{_render_header(ctx, engagement_url=engagement_url, dashboard_url=dashboard_url)}\n"
        '<main class="wrap">\n'
        f"{_render_summary(ctx)}\n"
        f"{_render_filter_bar(ctx)}\n"
        '<div id="sec-findings"></div>\n'
        f"{_render_groups(ctx, resolver)}\n"
        '<div id="sec-methodology"></div>\n'
        f"{_render_checklists(ctx)}\n"
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
    return _render_document(ctx, resolver, engagement_url=engagement_url, dashboard_url=dashboard_url)


def export_zip(ctx: ReportContext, artifact_bytes: ArtifactBytes | None) -> bytes:
    """Build a ZIP of ``report.html`` (assets externalized to ``artifacts/<name>``) + the referenced
    ``artifacts/`` files, for delivery without one giant inlined HTML file (PLAN.md §7)."""
    resolver = _AssetResolver("zip", artifact_bytes)
    html_doc = _render_document(ctx, resolver)
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
.topbar .wrap { display: flex; align-items: center; gap: 16px; height: 52px; }
.tb-brand { font-weight: 700; font-size: 14px; display: flex; gap: 8px; align-items: center; }
.tb-mark { width: 9px; height: 18px; border-radius: 2px; background: var(--accent); }
.topbar nav { display: flex; gap: 2px; margin-left: 4px; overflow-x: auto; }
.topbar nav a {
  font-size: 13px; font-weight: 550; color: var(--ink-2); text-decoration: none;
  padding: 6px 9px; border-radius: 6px; white-space: nowrap;
}
.topbar nav a:hover { background: var(--surface-2); color: var(--ink); }
.tb-actions { margin-left: auto; display: flex; gap: 8px; }
.btn {
  display: inline-flex; align-items: center; gap: 6px; font-family: inherit;
  font-size: 13px; font-weight: 650; cursor: pointer; border-radius: 7px;
  padding: 7px 13px; border: 1px solid transparent;
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
.report-nav { display: flex; gap: 14px; margin-bottom: 14px; font-size: 13px; font-weight: 600; }
.report-nav a { text-decoration: none; }
.report-nav a:hover { text-decoration: underline; }
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
  .topbar nav { display: none; }
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
  :root {
    --bg: #fff; --surface: #fff; --surface-2: #f4f6f8; --ink: #10202e; --ink-2: #33424f;
    --muted: #5a6b7b; --line: #dce2e8; --line-2: #c3ccd6;
  }
  body { font-size: 10.5pt; }
  .topbar, .filters, .sec-h .chev, .no-print, .toolbar, .report-nav { display: none !important; }
  .sec.collapsed .sec-body { display: block !important; }
  .finding, .metric, .risk, .evidence-item, .ck-item, .ck-table tr, .index-wrap { break-inside: avoid; }
  .children-table { break-inside: avoid; }
  .sec-h { break-after: avoid; }
  .finding-body .block-body pre { max-height: none; overflow: visible; }
  a { color: #10202e; text-decoration: underline; }
}
"""

_JS = """
(function () {
  "use strict";
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
