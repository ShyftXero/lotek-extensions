"""Editable ``.docx`` report renderer (WS8).

Consumes ``ReportContext`` (the FROZEN contract in ``scribble.reporting.context``) only — no DB
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

from scribble.content.render_docx import html_to_richtext
from scribble.enums import SEVERITY_ORDER as _ENUM_SEVERITY_ORDER
from scribble.reporting.context import ArtifactCtx, FindingCtx, GroupCtx, ReportContext

ArtifactBytes = Callable[[str], "bytes | None"]

SEVERITY_ORDER: tuple[str, ...] = tuple(s.value for s in _ENUM_SEVERITY_ORDER)  # worst-first

_TEMPLATE_PATH = Path(__file__).resolve().parent.parent / "report_templates" / "default.docx"

_BLOCK_LABELS = {"description": "Description", "remediation": "Remediation", "details": "Details"}
_BLOCK_ORDER = ("description", "remediation", "details")

# Same placeholder trick as ``render_html.make_inline_artifact_url`` (schemeless + relative so it
# survives ``content/render_html.py``'s sanitize pass), but WS8-owned so this module never has to
# reach into WS7's private constants.
_INLINE_PREFIX = "/__scribble_docx_inline__/"

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
    ``<details>``/table.

    A child's OWN artifacts render here too (issue #54, the docx half of ext#40 mechanism 2): before
    this, a screenshot attached to a promoted per-host instance never appeared in the docx report at
    all -- exactly the render_html gap ``_render_child_evidence_cell`` fixed on the HTML side. Each
    artifact becomes a placeholder ``<img>`` (this module's OWN ``make_inline_artifact_url``/
    ``_INLINE_PREFIX``, resolved by the ``image_resolver`` already threaded into ``html_to_richtext``
    -- see ``_finding_body_richtext``); a non-image or an image whose bytes are unavailable/oversized
    degrades to ``content/render_docx.py``'s existing bracketed placeholder rather than the render
    failing."""
    items = []
    for c in children:
        host = _html_escape(_child_host_label(c))
        summary = _child_summary_text(c)
        if summary:
            items.append(f"<li><strong>{host}</strong> — {_html_escape(summary)}</li>")
        else:
            items.append(f"<li><strong>{host}</strong></li>")
        for a in c.artifacts:
            src = make_inline_artifact_url(a.storage_path)
            if not src:
                continue
            cap = _html_escape(a.caption or a.filename)
            items.append(f'<li><img src="{src}" alt="{cap}"/> {cap}</li>')
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


def _append_checklists(doc, ctx: ReportContext) -> None:
    """Append the checklist sections to the RENDERED document with python-docx, rather than authoring a
    Jinja loop into the binary ``.docx`` template. Coverage/reminder -> a "Methodology and Coverage"
    section (status + note per item, grouped by section); compliance -> a "Compliance Attestation"
    section with a per-framework table (Control / Requirement / Result / Notes). Only checklists that
    opted into the report reach ``ctx.checklists``."""
    if not ctx.checklists:
        return
    coverage = [c for c in ctx.checklists if c.kind != "compliance"]
    compliance = [c for c in ctx.checklists if c.kind == "compliance"]

    def _rollup_line(cl) -> str:
        labels = [("satisfied", "Satisfied"), ("deficient", "Deficient"),
                  ("not_applicable", "N/A"), ("open", "Open")]
        return "   ".join(f"{lab}: {cl.rollup.get(k, 0)}" for k, lab in labels)

    if coverage:
        doc.add_heading("Methodology and Coverage", level=1)
        for cl in coverage:
            doc.add_heading(cl.name, level=2)
            doc.add_paragraph(_rollup_line(cl))
            last_section = object()
            for it in cl.items:
                if it.section != last_section:
                    last_section = it.section
                    if it.section:
                        doc.add_heading(it.section, level=3)
                p = doc.add_paragraph()
                p.add_run(f"[{(it.status or it.bucket_label)}] ").bold = True
                p.add_run(it.text)
                if it.finding_id and it.finding_title:
                    p.add_run(f"  (see: {it.finding_title})").italic = True
                if it.note:
                    doc.add_paragraph(it.note)

    if compliance:
        doc.add_heading("Compliance Attestation", level=1)
        for cl in compliance:
            doc.add_heading(cl.name, level=2)
            doc.add_paragraph(_rollup_line(cl))
            by_fw: dict[str, list] = {}
            fw_order: list[str] = []
            for it in cl.items:
                fw = it.framework or ""
                if fw not in by_fw:
                    by_fw[fw] = []
                    fw_order.append(fw)
                by_fw[fw].append(it)
            for fw in fw_order:
                if fw:
                    doc.add_heading(fw, level=3)
                table = doc.add_table(rows=1, cols=4)
                table.style = "Table Grid"
                hdr = table.rows[0].cells
                hdr[0].text, hdr[1].text, hdr[2].text, hdr[3].text = (
                    "Control", "Requirement", "Result", "Notes")
                for it in by_fw[fw]:
                    cells = table.add_row().cells
                    cells[0].text = it.control_ref or ""
                    cells[1].text = it.text
                    cells[2].text = it.bucket_label
                    note = it.note or ""
                    if it.finding_id and it.finding_title:
                        note = (note + f"  (see: {it.finding_title})").strip()
                    cells[3].text = note


def _append_evidence_appendix(doc, ctx: ReportContext, *, artifact_bytes: ArtifactBytes | None) -> None:
    """Append engagement-level evidence (``ReportContext.artifacts`` -- artifacts attached to the
    ENGAGEMENT with no ``finding_id``) to the RENDERED document, mirroring ``_append_checklists``
    (programmatic, post-render -- no Jinja loop authored into the binary template). This is the docx
    half of issue #54 / ext#40 mechanism 1: before this, such an upload was accepted, stored, answered
    201 with a URL, and then appeared in no docx deliverable at all -- the exact HTML-side gap
    ``_render_evidence_appendix`` (render_html.py) already closed.

    Only added when ``ctx.artifacts`` is non-empty, so an engagement with no engagement-level evidence
    renders exactly as it did before this section existed. Each item: an image within the size bound
    embeds via ``doc.add_picture``; anything else (non-image, oversized, or unavailable bytes) gets a
    caption/filename paragraph instead of the render failing. There is no zip delivery mode for docx
    (issue #62 does not apply here), so this is the one size bound this format needs."""
    if not ctx.artifacts:
        return
    doc.add_heading("Evidence Appendix", level=1)
    doc.add_paragraph(
        "Evidence recorded against this engagement as a whole rather than against one finding."
    )
    for a in ctx.artifacts:
        is_image = (a.content_type or "").startswith("image/")
        data: bytes | None = None
        if is_image and artifact_bytes:
            try:
                data = artifact_bytes(a.storage_path)
            except Exception:
                data = None
            if data and len(data) > _MAX_EVIDENCE_BYTES:
                data = None  # oversized: fall back to caption-only rather than embed a huge blob
        caption = a.caption or a.filename
        if data:
            try:
                doc.add_picture(io.BytesIO(data), width=_EVIDENCE_IMAGE_WIDTH)
                doc.add_paragraph(caption)
                continue
            except Exception:
                pass  # fall through to the caption-only path below
        p = doc.add_paragraph()
        p.add_run(f"{caption} ").italic = True
        p.add_run(f"({a.filename} -- not embedded)").italic = True


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
    _append_checklists(tpl.docx, ctx)  # programmatic, post-render (no Jinja in the binary template)
    _append_evidence_appendix(tpl.docx, ctx, artifact_bytes=artifact_bytes)

    buf = io.BytesIO()
    tpl.save(buf)
    return buf.getvalue()
