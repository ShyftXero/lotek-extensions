"""Editable ``.docx`` report renderer (WS8).

Consumes ``ReportContext`` (the FROZEN contract in ``fraction.reporting.context``) only — no DB
access, no Flask — mirroring WS7's HTML renderer. Loads ``report_templates/default.docx`` (a docxtpl
template authored by ``report_templates/build_default_docx.py``) and fills it with:

- Title-page + executive-summary scalars (company/engagement name, dates, severity rollup).
- Nested ``{% for group in groups %}`` / ``{% for f in group.findings %}`` loops (board order ==
  document order, exactly as the context already orders things).
- Each finding's content blocks (``FindingCtx.blocks_html`` — already variable-resolved, sanitized
  HTML) converted to a single :class:`docxtpl.RichText` via ``content/render_docx.py``'s HTML walker,
  bound to the template's ``{{r f.body }}`` field.
- Evidence-gallery artifacts embedded via :class:`docxtpl.InlineImage` (skipped gracefully — caption
  text only — when bytes aren't available or the artifact isn't an image).
- Per-severity coloring authored as Jinja conditionals *in the template itself*
  (``report_templates/build_default_docx.py``'s ``{% cellbg ... %}`` cell-shading expression) —
  replacing FACTION's `FAC701` sentinel hack per PLAN.md §9.

Inline (``inlineImage``/``figure``) images embedded *inside* prose round-trip through the same
placeholder-URL trick WS7 uses (see ``render_html.make_inline_artifact_url``), but with WS8's own
prefix so this module never depends on WS7's private internals: callers build the ``ReportContext``
with ``artifact_url=`` wired to :func:`make_inline_artifact_url` from *this* module for those images
to be embeddable here.
"""

from __future__ import annotations

import io
from collections.abc import Callable
from datetime import UTC, datetime
from html import escape as _html_escape
from pathlib import Path
from urllib.parse import quote, unquote

from docx.shared import Mm
from docxtpl import DocxTemplate, InlineImage, RichText

from fraction.content.render_docx import html_to_richtext
from fraction.enums import SEVERITY_ORDER as _ENUM_SEVERITY_ORDER
from fraction.reporting.context import ArtifactCtx, FindingCtx, GroupCtx, ReportContext

ArtifactBytes = Callable[[str], "bytes | None"]

SEVERITY_ORDER: tuple[str, ...] = tuple(s.value for s in _ENUM_SEVERITY_ORDER)  # worst-first

_TEMPLATE_PATH = Path(__file__).resolve().parent.parent / "report_templates" / "default.docx"

_BLOCK_LABELS = {"description": "Description", "remediation": "Remediation", "details": "Details"}
_BLOCK_ORDER = ("description", "remediation", "details")

# Same placeholder trick as ``render_html.make_inline_artifact_url`` (schemeless + relative so it
# survives ``content/render_html.py``'s sanitize pass), but WS8-owned so this module never has to
# reach into WS7's private constants.
_INLINE_PREFIX = "/__fraction_docx_inline__/"

_EVIDENCE_IMAGE_WIDTH = Mm(70)

# Don't embed an evidence image whose bytes exceed this — fall back to the caption-only rendering — so
# a single huge artifact can't blow up the render's memory footprint (matches the cap in
# ``content/render_docx.py`` and the stat-based guard in ``report_docx_api``). 25 MiB.
_MAX_EVIDENCE_BYTES = 25 * 1024 * 1024


def make_inline_artifact_url(storage_path: str | None) -> str:
    """Placeholder ``src`` for an inline content-node image, to pass as (or wire into) the
    ``artifact_url`` callback given to ``build_report_context`` for WS8 rendering."""
    if not storage_path:
        return ""
    return _INLINE_PREFIX + quote(storage_path, safe="")


def _decode_inline_placeholder(src: str | None) -> str | None:
    if not src or not src.startswith(_INLINE_PREFIX):
        return None
    return unquote(src[len(_INLINE_PREFIX) :]) or None


def _make_image_resolver(artifact_bytes: ArtifactBytes | None) -> Callable[[str], bytes | None]:
    def _resolve(src: str) -> bytes | None:
        if not artifact_bytes:
            return None
        storage_path = _decode_inline_placeholder(src) or src
        try:
            return artifact_bytes(storage_path)
        except Exception:
            return None

    return _resolve


def _label_run_xml(text: str) -> str:
    rt = RichText()
    rt.add(text)
    return rt.xml


def _child_host_label(c: FindingCtx) -> str:
    if c.target_host:
        return c.target_host + (f":{c.target_port}" if c.target_port else "")
    if c.target_url:
        return c.target_url
    return c.title


def _child_summary_text(c: FindingCtx) -> str:
    """The per-host evidence line for a child finding's "Affected Hosts" list entry — mirrors
    ``render_html._child_summary_text``: built from the child's OWN ``variables`` overlay
    (``FindingCtx.facts_line``), not from its content blocks, since every child promoted under the same
    vuln-DB template shares that template's ``content_json`` verbatim with its parent."""
    return c.facts_line


def _children_html(children: list[FindingCtx]) -> str:
    """A small HTML fragment (fed through the same ``html_to_richtext`` walker as content blocks) —
    the "Affected Hosts" compact per-host list, rendered INLINE into the parent finding's body since
    the ``.docx`` template's finding loop is authored as a flat paragraph sequence
    (``build_default_docx.py``), not a per-finding sub-scope a nested ``{%tr for %}`` row-loop could
    cleanly hang off of. This is the DOCX-side mirror of ``render_html._render_children``'s
    ``<details>``/table."""
    items = []
    for c in children:
        host = _html_escape(_child_host_label(c))
        summary = _child_summary_text(c)
        if summary:
            items.append(f"<li><strong>{host}</strong> — {_html_escape(summary)}</li>")
        else:
            items.append(f"<li><strong>{host}</strong></li>")
    return f"<h4>Affected Hosts ({len(children)})</h4><ul>{''.join(items)}</ul>"


def _finding_body_richtext(
    tpl: DocxTemplate,
    blocks_html: dict[str, str],
    image_resolver: Callable[[str], bytes | None],
    *,
    children: list[FindingCtx] | None = None,
) -> RichText:
    """Combine a finding's content blocks into one RichText, each under a "Heading 3" label —
    mirrors ``render_html._render_blocks``'s label + ordering behavior. ``children`` (a nested
    finding's per-host instances) render as an "Affected Hosts" list appended to the same body."""
    parts: list[str] = []
    any_open = False

    def _open(style: str | None = None) -> None:
        nonlocal any_open
        prefix = "</w:p><w:p>" if any_open else ""
        ppr = f'<w:pPr><w:pStyle w:val="{style}"/></w:pPr>' if style else ""
        parts.append(prefix + ppr)
        any_open = True

    seen: set[str] = set()
    ordered_keys = [k for k in _BLOCK_ORDER] + [k for k in blocks_html if k not in _BLOCK_ORDER]
    rendered_any = False
    for key in ordered_keys:
        if key in seen:
            continue
        seen.add(key)
        fragment = blocks_html.get(key)
        if not fragment:
            continue
        rendered_any = True
        label = _BLOCK_LABELS.get(key, key.replace("_", " ").title())
        _open(style="Heading3")  # styleId (spaceless) — <w:pStyle> resolves by id, not display name
        parts.append(_label_run_xml(label))
        block_rt = html_to_richtext(fragment, tpl=tpl, image_resolver=image_resolver)
        if block_rt.xml:
            parts.append("</w:p><w:p>")
            parts.append(block_rt.xml)
            any_open = True

    if not rendered_any and not children:
        _open()
        parts.append(_label_run_xml("No content."))

    if children:
        if not any_open:
            _open()
        children_rt = html_to_richtext(_children_html(children), tpl=tpl, image_resolver=image_resolver)
        if children_rt.xml:
            parts.append("</w:p><w:p>")
            parts.append(children_rt.xml)
            any_open = True

    rt = RichText()
    rt.xml = "".join(parts)
    return rt


def _target_text(f: FindingCtx) -> str:
    bits: list[str] = []
    if f.target_host:
        bits.append(f.target_host + (f":{f.target_port}" if f.target_port else ""))
    if f.target_url:
        bits.append(f.target_url)
    return " · ".join(bits)


def _artifact_ctx(
    a: ArtifactCtx, artifact_bytes: ArtifactBytes | None, tpl: DocxTemplate
) -> dict[str, object]:
    is_image = (a.content_type or "").startswith("image/")
    image = None
    if is_image and artifact_bytes:
        try:
            data = artifact_bytes(a.storage_path)
        except Exception:
            data = None
        if data and len(data) > _MAX_EVIDENCE_BYTES:
            data = None  # oversized: fall back to caption-only rather than embed a huge blob
        if data:
            try:
                image = InlineImage(tpl, io.BytesIO(data), width=_EVIDENCE_IMAGE_WIDTH)
            except Exception:
                image = None
    return {
        "caption": a.caption or a.filename,
        "filename": a.filename,
        "image": image,
        "embedded": image is not None,
    }


def _finding_ctx(
    f: FindingCtx, *, tpl: DocxTemplate, artifact_bytes: ArtifactBytes | None
) -> dict[str, object]:
    image_resolver = _make_image_resolver(artifact_bytes)
    sev = f.severity if f.severity in SEVERITY_ORDER else "info"
    return {
        "title": f.title,
        "severity": sev,
        "severity_label": sev.title(),
        "cvss_score": f"{f.cvss_score:.1f}" if f.cvss_score is not None else "",
        "cvss_vector": f.cvss_vector or "",
        "target": _target_text(f),
        "body": _finding_body_richtext(tpl, f.blocks_html, image_resolver, children=f.children),
        "artifacts": [_artifact_ctx(a, artifact_bytes, tpl) for a in f.artifacts],
    }


def _group_ctx(g: GroupCtx, *, tpl: DocxTemplate, artifact_bytes: ArtifactBytes | None) -> dict:
    return {
        "name": g.name,
        "type_slug": g.type_slug or "",
        "findings": [_finding_ctx(f, tpl=tpl, artifact_bytes=artifact_bytes) for f in g.findings],
    }


def _build_context(ctx: ReportContext, *, tpl: DocxTemplate, artifact_bytes: ArtifactBytes | None) -> dict:
    rollup = ctx.rollup
    counts = rollup.counts if rollup else {}
    overall = rollup.overall if rollup else "info"
    total = rollup.total if rollup else 0
    return {
        "company_name": ctx.company_name or "",
        "engagement_name": ctx.engagement_name,
        "client_name": ctx.client_name or "",
        "scope_type": ctx.scope_type or "",
        "start_date": ctx.start_date or "",
        "end_date": ctx.end_date or "",
        "generated_date": datetime.now(UTC).strftime("%Y-%m-%d"),
        "narrative": ctx.narrative or "",
        "rollup": {
            "counts": {s: counts.get(s, 0) for s in SEVERITY_ORDER},
            "total": total,
            "overall": overall,
            "overall_label": "No Findings" if total == 0 else overall.title(),
        },
        "groups": [_group_ctx(g, tpl=tpl, artifact_bytes=artifact_bytes) for g in ctx.groups],
    }


def render_report_docx(ctx: ReportContext, *, artifact_bytes: ArtifactBytes | None = None) -> bytes:
    """Render ``ctx`` to a ``.docx`` document (bytes) using ``report_templates/default.docx``.

    ``artifact_bytes(storage_path) -> bytes | None`` supplies evidence-gallery + inline-content image
    bytes; when ``None`` (or a lookup fails), images degrade to caption-only / bracketed-placeholder
    text rather than the render failing.
    """
    tpl = DocxTemplate(str(_TEMPLATE_PATH))
    tpl.init_docx()
    # Content-block conversion happens eagerly (before ``tpl.render()``), so inline images need
    # ``current_rendering_part`` set now — this is the same part ``render()`` would assign for the
    # main document body, so it's consistent with what happens automatically for everything else
    # (scalars, evidence ``InlineImage``s) during the subsequent ``tpl.render()`` call.
    tpl.current_rendering_part = tpl.docx.part

    context = _build_context(ctx, tpl=tpl, artifact_bytes=artifact_bytes)
    tpl.render(context)

    buf = io.BytesIO()
    tpl.save(buf)
    return buf.getvalue()
