"""Self-contained HTML report renderer (WS7).

Consumes ``ReportContext`` (the FROZEN contract in ``fraction.reporting.context``) only — no DB access,
no Flask. Produces a standalone HTML document: its own inline ``<style>``/``<script>``, no external
hosts, so it is deliverable on its own and prints cleanly to PDF. Aesthetic matches Lotek's
``report.html`` (dark theme, ``--accent`` green, ``--sev-*`` ramp, collapsible sections, gradient
header) using Fraction's own token values (``fraction/static/fraction.css``).

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

from fraction.enums import SEVERITY_ORDER as _ENUM_SEVERITY_ORDER
from fraction.reporting.context import ArtifactCtx, FindingCtx, GroupCtx, ReportContext

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
_INLINE_PREFIX = "/__fraction_inline__/"
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
    ``fraction.promote.promote_job``), so a per-child excerpt of ``blocks_html`` would just repeat the
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
    eyebrow = _esc(ctx.client_name or ctx.company_name or "Security Assessment")
    subtitle = f"{_esc(ctx.scope_type)} assessment" if ctx.scope_type else "Penetration test report"
    dates = ""
    if ctx.start_date or ctx.end_date:
        dates = (
            f'<div class="dates muted">{_esc(ctx.start_date or "?")} – '
            f'{_esc(ctx.end_date or "present")}</div>'
        )
    nav_links: list[str] = []
    if dashboard_url:
        nav_links.append(f'<a href="{_esc(dashboard_url)}">← Dashboard</a>')
    if engagement_url:
        nav_links.append(f'<a href="{_esc(engagement_url)}">← Back to engagement</a>')
    nav_html = f'<nav class="report-nav no-print">{"".join(nav_links)}</nav>' if nav_links else ""
    return (
        '<header class="report-head"><div class="wrap headwrap">'
        f"{nav_html}"
        '<div class="brandrow"><div class="brand">'
        f'<div class="eyebrow">{eyebrow}</div><h1>{_esc(ctx.engagement_name)}</h1>'
        f'<div class="muted">{subtitle}</div>{dates}</div>'
        '<div class="confidential">Confidential</div></div>'
        '<div class="toolbar no-print">'
        '<button class="btn" type="button" data-action="print">⎙ Print / Save as PDF</button>'
        '<button class="btn ghost" type="button" data-action="expand-all">Expand all</button>'
        '<button class="btn ghost" type="button" data-action="collapse-all">Collapse all</button>'
        "</div></div></header>"
    )


def _render_summary(ctx: ReportContext) -> str:
    rollup = ctx.rollup
    counts = rollup.counts if rollup else {}
    total = rollup.total if rollup else 0
    overall = rollup.overall if rollup else "info"
    clean = total == 0
    banner_class = "risk-clean" if clean else f"risk-{overall}"
    banner_label = "No Findings" if clean else overall.title()

    kpis = "".join(
        f'<div class="kv"><div class="k">{s.title()}</div>'
        f'<div class="v"><span class="sev-badge sev-{s}">{counts.get(s, 0)}</span></div></div>'
        for s in SEVERITY_ORDER
    )
    n_groups = len(ctx.groups)
    # ``rollup.total`` (not a plain sum over ``ctx.groups[].findings``) so this tile still reflects
    # every finding INSTANCE -- nested children collapse out of ``findings`` once ``build_report_context``
    # groups them under their parent, but they're still real findings for the purpose of this count.
    n_findings = total
    assessor = ctx.variables.get("ASSESSOR", "") if ctx.variables else ""
    assessor_kv = (
        f'<div class="kv"><div class="k">Assessor</div><div class="v">{_esc(assessor)}</div></div>'
        if assessor
        else ""
    )
    narrative_html = f'<p class="summary-narrative">{_esc(ctx.narrative)}</p>' if ctx.narrative else ""
    return (
        '<section class="sec" id="sec-summary">'
        '<h2 class="sec-h">Executive Summary <span class="chev">▾</span></h2>'
        '<div class="sec-body">'
        f'<div class="risk {banner_class}"><div class="dot"></div>'
        f'<div><div class="label">Overall Risk</div><div class="level">{_esc(banner_label)}</div></div></div>'
        f"{narrative_html}"
        f'<div class="grid kpis">{kpis}</div>'
        '<div class="grid">'
        f'<div class="kv"><div class="k">Total Findings</div><div class="v">{n_findings}</div></div>'
        f'<div class="kv"><div class="k">Sections</div><div class="v">{n_groups}</div></div>'
        f'<div class="kv"><div class="k">Scope</div><div class="v">{_esc(ctx.scope_type or "—")}</div></div>'
        f"{assessor_kv}"
        "</div></div></section>"
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
    return f'<div class="foot">Generated by Fraction · engagement #{ctx.engagement_id} · {generated}</div>'


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
        f"{_render_groups(ctx, resolver)}\n"
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
    and the Fraction dashboard -- the report -> app cross-links half of D3 nav (the app -> report
    direction lives in ``fraction/templates/fraction/engagement.html``'s ``View Report`` button).
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
:root {
  --bg: #060b12; --bg2: #0f1620; --card: #131f2c;
  --ink: #dce7f3; --muted: #8fa4b8;
  --accent: #22c55e; --accent-strong: #16a34a;
  --line: #2a3b4d; --line-strong: #3a4f66;
  --shadow: rgba(0, 0, 0, 0.35);
  --sev-critical: #b91c1c; --sev-high: #dc2626; --sev-medium: #ea580c;
  --sev-low: #ca8a04; --sev-info: #0284c7;
}
* { box-sizing: border-box; }
body {
  margin: 0; background: var(--bg); color: var(--ink);
  font-family: "Inter", "IBM Plex Sans", "Segoe UI", sans-serif;
  font-size: 15px; line-height: 1.55;
}
.wrap { max-width: 1100px; margin: 0 auto; padding: 0 22px 60px; }
h1, h2, h3 { margin: 0 0 4px; }
a { color: var(--accent); }
.muted { color: var(--muted); }

.report-head {
  background: linear-gradient(135deg, #0e1828, #152133);
  border-bottom: 1px solid var(--line); padding: 26px 0 20px; margin-bottom: 6px;
}
.report-head .wrap { padding-bottom: 0; }
.report-nav {
  display: flex; gap: 14px; margin-bottom: 14px; font-size: 13px; font-weight: 600;
}
.report-nav a { text-decoration: none; }
.report-nav a:hover { text-decoration: underline; }
.brandrow {
  display: flex; justify-content: space-between; align-items: flex-start;
  gap: 16px; flex-wrap: wrap;
}
.eyebrow {
  font-size: 13px; text-transform: uppercase; letter-spacing: .12em;
  color: var(--accent); font-weight: 700; margin-bottom: 2px;
}
.brand h1 { font-size: 26px; }
.dates { margin-top: 4px; font-size: 13px; }
.confidential {
  font-size: 11px; text-transform: uppercase; letter-spacing: .1em; color: #ffb3b3;
  border: 1px solid #7f1d2b; background: #2a0d12; border-radius: 6px;
  padding: 4px 10px; white-space: nowrap;
}
.toolbar { margin-top: 16px; display: flex; gap: 8px; flex-wrap: wrap; }
.btn {
  display: inline-flex; align-items: center; gap: 6px; background: var(--accent);
  color: #04210f; border: none; border-radius: 8px; padding: 8px 14px;
  font-weight: 700; font-size: 13px; cursor: pointer; font-family: inherit;
}
.btn.ghost { background: transparent; color: var(--ink); border: 1px solid var(--line-strong); }
.btn.ghost:hover { border-color: var(--accent); color: #fff; }

section.sec { margin: 0; }
.sec-h {
  font-size: 16px; margin: 28px 0 12px; border-bottom: 1px solid var(--line); padding-bottom: 6px;
  text-transform: uppercase; letter-spacing: .06em; color: var(--muted);
  display: flex; align-items: center; gap: 8px; cursor: pointer; user-select: none;
}
.sec-h:hover { color: var(--ink); }
.sec-h .chev { transition: transform .15s ease; font-size: 12px; color: var(--accent); }
.sec-h .count {
  font-size: 12px; font-weight: 400; text-transform: none; letter-spacing: 0;
  color: var(--muted); margin-left: auto;
}
.sec.collapsed .sec-h .chev { transform: rotate(-90deg); }
.sec.collapsed .sec-body { display: none; }
.swatch { display: inline-block; width: 10px; height: 10px; border-radius: 3px; }

.risk {
  display: flex; align-items: center; gap: 14px; border-radius: 12px; padding: 14px 18px;
  margin: 4px 0 16px; border: 1px solid var(--line-strong); background: var(--card);
}
.risk .dot { width: 44px; height: 44px; border-radius: 50%; flex: 0 0 auto; }
.risk .label { font-size: 11px; text-transform: uppercase; letter-spacing: .08em; color: var(--muted); }
.risk .level { font-size: 20px; font-weight: 800; }
.risk-critical .dot { background: var(--sev-critical); }
.risk-critical .level { color: var(--sev-critical); }
.risk-high .dot { background: var(--sev-high); }
.risk-high .level { color: var(--sev-high); }
.risk-medium .dot { background: var(--sev-medium); }
.risk-medium .level { color: var(--sev-medium); }
.risk-low .dot { background: var(--sev-low); }
.risk-low .level { color: var(--sev-low); }
.risk-info .dot { background: var(--sev-info); }
.risk-info .level { color: var(--sev-info); }
.risk-clean .dot { background: var(--accent); }
.risk-clean .level { color: var(--accent); }

.grid {
  display: grid; gap: 10px; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
  margin: 14px 0;
}
.grid.kpis { grid-template-columns: repeat(5, minmax(90px, 1fr)); }
.kv { background: var(--card); border: 1px solid var(--line); border-radius: 10px; padding: 10px 12px; }
.kv .k { color: var(--muted); font-size: 11px; text-transform: uppercase; letter-spacing: .04em; }
.kv .v { font-size: 15px; margin-top: 2px; word-break: break-word; }

.sev-badge {
  display: inline-block; padding: 2px 10px; border-radius: 999px; font-size: 11px;
  font-weight: 700; text-transform: uppercase; letter-spacing: .03em; color: #fff;
}
.sev-badge.sev-critical { background: var(--sev-critical); }
.sev-badge.sev-high { background: var(--sev-high); }
.sev-badge.sev-medium { background: var(--sev-medium); }
.sev-badge.sev-low { background: var(--sev-low); color: #1a1a1a; }
.sev-badge.sev-info { background: var(--sev-info); }

.filters { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; margin: 8px 0 18px; }
.filters input[type=search] {
  background: var(--bg2); border: 1px solid var(--line); border-radius: 8px; color: var(--ink);
  padding: 7px 10px; font-size: 13px; min-width: 220px; flex: 1 1 220px;
}
.chip-toggle {
  cursor: pointer; user-select: none; border: 1px solid var(--line-strong); border-radius: 999px;
  padding: 3px 11px; font-size: 11px; font-weight: 700; text-transform: uppercase;
  letter-spacing: .03em; opacity: .45; color: var(--ink);
}
.chip-toggle.on { opacity: 1; }
.chip-toggle.sev-critical.on { border-color: var(--sev-critical); color: var(--sev-critical); }
.chip-toggle.sev-high.on { border-color: var(--sev-high); color: var(--sev-high); }
.chip-toggle.sev-medium.on { border-color: var(--sev-medium); color: var(--sev-medium); }
.chip-toggle.sev-low.on { border-color: var(--sev-low); color: var(--sev-low); }
.chip-toggle.sev-info.on { border-color: var(--sev-info); color: var(--sev-info); }
.sort-toggle { font-size: 12px; color: var(--muted); display: flex; align-items: center; gap: 5px; }
.filter-count { font-size: 12px; color: var(--muted); margin-left: auto; }

.finding {
  background: var(--card); border: 1px solid var(--line); border-left-width: 4px;
  border-left-color: var(--line-strong); border-radius: 10px; padding: 14px 16px; margin: 12px 0;
}
.finding.sev-critical { border-left-color: var(--sev-critical); }
.finding.sev-high { border-left-color: var(--sev-high); }
.finding.sev-medium { border-left-color: var(--sev-medium); }
.finding.sev-low { border-left-color: var(--sev-low); }
.finding.sev-info { border-left-color: var(--sev-info); }
.finding.filtered-out { display: none; }
.finding-head {
  display: flex; justify-content: space-between; align-items: flex-start;
  gap: 10px; flex-wrap: wrap;
}
.finding h3 { font-size: 15px; }
.finding-badges { display: flex; gap: 6px; align-items: center; flex-wrap: wrap; }
.finding-meta { display: flex; flex-wrap: wrap; gap: 8px; margin: 8px 0; }
.chip {
  display: inline-block; padding: 2px 10px; border-radius: 999px; font-size: 12px;
  background: var(--bg2); border: 1px solid var(--line); color: var(--muted);
}
.chip.cvss { color: var(--ink); }
.finding-body .block { margin-top: 10px; }
.finding-body .block-label {
  font-size: 11px; text-transform: uppercase; letter-spacing: .05em;
  color: var(--muted); margin-bottom: 2px;
}
.finding-body .block-body p { margin: 4px 0; }
.finding-body .block-body img { max-width: 100%; border-radius: 6px; }
.empty { color: var(--muted); font-style: italic; }

.summary-narrative {
  color: var(--ink); font-size: 14px; line-height: 1.6; margin: 2px 0 16px; max-width: 76ch;
}

.children { margin-top: 12px; border: 1px solid var(--line); border-radius: 8px; background: var(--bg2); }
.children summary {
  cursor: pointer; user-select: none; padding: 8px 12px; font-size: 12px; font-weight: 700;
  text-transform: uppercase; letter-spacing: .04em; color: var(--muted);
}
.children summary:hover { color: var(--ink); }
.children-table { width: 100%; border-collapse: collapse; font-size: 13px; }
.children-table th, .children-table td {
  padding: 6px 12px; border-top: 1px solid var(--line); text-align: left; vertical-align: top;
}
.children-table th { color: var(--muted); font-weight: 600; font-size: 11px; text-transform: uppercase; }
.children-table td.child-host { white-space: nowrap; font-weight: 600; }
.children-table td.child-evidence { color: var(--muted); }

.evidence { margin-top: 12px; }
.evidence-label {
  font-size: 11px; text-transform: uppercase; letter-spacing: .05em;
  color: var(--muted); margin-bottom: 6px;
}
.evidence-grid { display: grid; gap: 10px; grid-template-columns: repeat(auto-fill, minmax(180px, 1fr)); }
.evidence-item {
  border: 1px solid var(--line); border-radius: 8px; overflow: hidden;
  background: var(--bg2); margin: 0;
}
.evidence-item img { display: block; width: 100%; height: 130px; object-fit: cover; background: #0a0f17; }
.evidence-item figcaption, .evidence-item .cap {
  padding: 5px 8px; font-size: 11px; color: var(--muted); word-break: break-word;
}
.evidence-item.file { padding: 10px; font-size: 13px; }
.evidence-item.missing { padding: 10px; font-size: 13px; color: var(--muted); }
.evidence-link:hover { opacity: .85; }
.file-chip { color: var(--ink); text-decoration: none; }

.lightbox { display: none; }
.lightbox:target {
  display: flex; position: fixed; inset: 0; z-index: 1000; align-items: center;
  justify-content: center; padding: 24px; background: rgba(3,8,15,.92); cursor: zoom-out;
}
.lightbox img {
  max-width: 100%; max-height: 100%; object-fit: contain; box-shadow: 0 8px 40px rgba(0,0,0,.6);
}

.foot {
  margin-top: 40px; padding-top: 12px; border-top: 1px solid var(--line);
  font-size: 12px; color: var(--muted);
}

/* checklists (coverage / compliance) */
.ck-list {
  background: var(--card); border: 1px solid var(--line); border-radius: 10px;
  padding: 12px 16px; margin: 12px 0;
}
.ck-head {
  display: flex; justify-content: space-between; align-items: baseline; gap: 10px; flex-wrap: wrap;
}
.ck-head h3 { font-size: 15px; }
.ck-rollup { display: flex; gap: 6px; flex-wrap: wrap; }
.ck-chip {
  font-size: 11px; padding: 2px 9px; border-radius: 999px;
  border: 1px solid var(--line-strong); color: var(--muted);
}
.ck-section {
  font-size: 11px; text-transform: uppercase; letter-spacing: .05em; color: var(--muted);
  margin: 12px 0 4px; border-bottom: 1px solid var(--line); padding-bottom: 3px;
}
.ck-item {
  display: flex; gap: 10px; align-items: flex-start; padding: 5px 0;
  border-top: 1px solid var(--line);
}
.ck-item:first-of-type { border-top: none; }
.ck-item-body { flex: 1; }
.ck-item-text { font-size: 14px; }
.ck-note { font-size: 12px; color: var(--muted); margin-top: 2px; }
.ck-finding { font-size: 12px; }
.ck-badge {
  display: inline-block; min-width: 74px; text-align: center; padding: 2px 8px;
  border-radius: 6px; font-size: 11px; font-weight: 700; text-transform: uppercase;
  letter-spacing: .02em; color: #fff; flex: 0 0 auto;
}
.ck-satisfied { background: var(--accent-strong); }
.ck-deficient { background: var(--sev-high); }
.ck-not_applicable { background: var(--line-strong); color: var(--ink); }
.ck-open { background: var(--sev-info); }
.ck-chip.ck-satisfied { border-color: var(--accent-strong); color: var(--accent); }
.ck-chip.ck-deficient { border-color: var(--sev-high); color: var(--sev-high); }
.ck-chip.ck-not_applicable { border-color: var(--line-strong); }
.ck-chip.ck-open { border-color: var(--sev-info); color: var(--sev-info); }
.ck-tablewrap { overflow-x: auto; }
.ck-table { width: 100%; border-collapse: collapse; font-size: 13px; margin: 4px 0 10px; }
.ck-table th, .ck-table td {
  padding: 6px 10px; border-top: 1px solid var(--line); text-align: left; vertical-align: top;
}
.ck-table th { color: var(--muted); font-size: 11px; text-transform: uppercase; font-weight: 600; }
.ck-table td.ck-ctrl { white-space: nowrap; font-weight: 600; font-variant-numeric: tabular-nums; }

@media (max-width: 720px) {
  .grid.kpis { grid-template-columns: repeat(2, 1fr); }
}

@media print {
  .lightbox, .lightbox:target { display: none !important; }
  :root {
    --bg: #fff; --bg2: #fff; --card: #fff; --ink: #10202e; --muted: #5a6b7b;
    --line: #d6dde6; --line-strong: #c3ccd6;
  }
  body { font-size: 11pt; }
  .report-head { background: #fff; border-bottom: 2px solid #10202e; }
  .no-print, .toolbar, .sec-h .chev, .filters { display: none !important; }
  .sec.collapsed .sec-body { display: block !important; }
  .finding, .kv, .risk, .evidence-item { break-inside: avoid; }
  .ck-item, .ck-table tr { break-inside: avoid; }
  .sec-h { break-after: avoid; }
  .children-table { break-inside: avoid; }
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
