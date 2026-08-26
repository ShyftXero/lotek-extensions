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
import json
import re
from collections.abc import Callable
from datetime import UTC, datetime
from html import escape as _html_escape
from pathlib import Path
from urllib.parse import quote, unquote

from docx.shared import Mm
from docxtpl import DocxTemplate, InlineImage, RichText

from scribble.content.render_docx import html_to_richtext
from scribble.enums import SEVERITY_ORDER as _ENUM_SEVERITY_ORDER
from scribble.reporting.context import (
    ArtifactCtx,
    FindingCtx,
    GroupCtx,
    ReportContext,
    figure_caption,
)

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
            cap = _html_escape(_numbered_caption(a))  # ext#117 — same "Figure N" the HTML prints
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


def _numbered_caption(a: ArtifactCtx) -> str:
    """``"Figure 3 — Payload firing in the browser"`` (ext#117). The number comes off the CONTEXT
    (``context.number_figures``), never from a counter this renderer keeps, so it is the same number
    the HTML deliverable prints for the same artifact."""
    return _xml_safe(figure_caption(a.figure_number, a.caption or a.filename))


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
        "caption": _numbered_caption(a),
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


# ── attack paths (ext#115) ───────────────────────────────────────────────────────────────────────────
#
# The HTML deliverable embeds vector's self-contained ``export.html`` in a sandboxed iframe and the
# animation plays. Word has no browser, so until ext#115 the .docx simply had no attack path in it at
# all: `Attack path` did not appear in `word/document.xml`, and a reader given only the .docx could not
# tell that a diagram existed. That is a SILENT content drop between two deliverables of the same
# engagement, which is worse than an ugly rendition.
#
# Why a native Word table and not a picture: a picture in a .docx must be RASTER (python-docx/docxtpl
# reject SVG; Word's `svgBlip` needs a PNG fallback anyway), and vector's diagram only exists as pixels
# once a browser has run `vector-viewer.js`. Rasterizing it server-side means shipping a headless
# browser or a rasterizer into a mounted production extension; re-drawing it in Python means a SECOND
# renderer that drifts from the JS the first time anyone edits the viewer. So this draws the same
# geometry the viewer draws -- `zone` is the column, `row` is the row (vector-viewer.js `geometry()`) --
# with Word's own table, plus the phase walkthrough and the edge list. Static, selectable, searchable,
# and it cannot drift out of a font metric.
#
# ponytail: table rendition, not pixels. Upgrade path if a true still is ever wanted: have vector's
# viewer serialize its live <svg> at export time (`XMLSerializer`, ~5 lines, zero drift) and store it
# alongside `embed_html`, then rasterize THAT here.

# ``embed_html`` is operator/agent-supplied (it arrives over a PAT POST -- see
# ``api_pat.scribble_link_attack_path``) and is stored verbatim, so nothing guarantees it came from
# vector or is bounded the way vector's own schema bounds a model. Every number below is scribble's own
# cap on how much of a snapshot this renderer will turn into document, independent of vector's limits.
_MAX_DIAGRAM_SCAN_CHARS = 8 * 1024 * 1024  # don't scan an unbounded snapshot at all
_MAX_DIAGRAMS = 50  # a report section, not an archive (cf. render_html's _MAX_APPENDIX_ITEMS)
_MAX_DIAGRAM_ZONES = 12  # a Word table wider than this is unreadable on a portrait page anyway
_MAX_DIAGRAM_ROWS = 40
_MAX_DIAGRAM_NODES = 2000
_MAX_DIAGRAM_PHASES = 60
_MAX_DIAGRAM_EDGES = 80
_MAX_DIAGRAM_TEXT = 400  # per field, matching vector's own _MED/_LONG spirit

# Codepoints legal in a Python str and legal in JSON, but ILLEGAL in XML 1.0 -- lxml (under
# python-docx) raises ``ValueError`` on any of them, which would turn one poisoned diagram into an
# uncaught 500 on every ``.docx`` export of that engagement, forever. They cannot be filtered at the
# link route: the JSON blob carries them as the six ASCII characters ``\u0000``, so the stored
# ``embed_html`` holds no literal control byte for ``api_pat._nul_safe`` to strip, and ``json.loads``
# materializes the real character here. (Tab/LF/CR are legal in XML and are collapsed by ``_d_str``'s
# whitespace split anyway.)
_XML_ILLEGAL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _xml_safe(text: str) -> str:
    """Drop the characters python-docx cannot serialize. Applied at the TWO places untrusted text
    becomes document -- here and in :func:`_numbered_caption` -- not at each ``add_run`` call."""
    return _XML_ILLEGAL_RE.sub("", text)


def _d_str(value: object, cap: int = _MAX_DIAGRAM_TEXT) -> str:
    """A capped, single-line, XML-safe string from an untrusted model field; non-scalar -> ``""``."""
    if value is None or isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return ""
    return " ".join(_xml_safe(str(value)).split())[:cap]


def _d_int(value: object, default: int = 0) -> int:
    try:
        if isinstance(value, bool):
            return default
        return int(value)
    except (TypeError, ValueError, OverflowError):
        # OverflowError is an ArithmeticError, NOT a ValueError: ``int(float("inf"))`` raises it, and
        # JSON's non-standard ``Infinity`` / an overflowing ``1e999`` both produce that float. Missing
        # it made a one-token payload an uncaught 500 on the whole deliverable.
        return default


def _d_list(value: object) -> list:
    return value if isinstance(value, list) else []


def _find_model_blob(embed_html: str) -> str | None:
    """The text inside ``<script … id="vap-model">…</script>``, by LINEAR scans.

    This was a regex (``<script\\b[^>]*\\bid=["']vap-model["'][^>]*>(.*?)</script>``) and that was a
    denial of service: two unanchored ``[^>]*`` runs before a literal mean every ``<script`` in the
    input is a start position costing O(n), so the match is O(n²) in a string an operator PAT chooses.
    Measured on the real pattern: ``"<script " * 8000`` (64 KiB) took **15.9 s**, and the curve
    extrapolates to ~71 minutes at 1 MiB -- well inside the 10 MiB the link route accepts. CPython's
    ``re`` holds the GIL and never yields to the gevent hub, so that is not a slow request, it is the
    whole worker. Lowering the size cap does not fix a quadratic; removing the backtracking does.
    (Re-measured after: 8 MiB of the same payload, 0.02 s.)

    Deliberately strict about what it accepts, because the job is to read VECTOR's output, not to
    parse hostile HTML: the id attribute must sit inside a ``<script`` tag that opens within 256
    characters before it, and the body ends at the first ``</script``. vector's ``json_for_script``
    escapes ``<``/``>``/``&`` as ``\\uXXXX``, so a genuine model never contains either."""
    for needle in ('id="vap-model"', "id='vap-model'"):
        at = embed_html.find(needle)
        if at < 0:
            continue
        start = embed_html.rfind("<script", max(0, at - 256), at)
        if start < 0:
            continue
        body = embed_html.find(">", at)
        if body < 0:
            continue
        end = embed_html.find("</script", body + 1)
        if end < 0:
            continue
        return embed_html[body + 1 : end]
    return None


def _diagram_model(embed_html: str | None) -> dict | None:
    """The ``vector.attackpath/v1`` document carried inside a stored snapshot, or ``None``.

    Reads a JSON blob out of HTML scribble already stores -- it does not import vector (extensions stay
    independent -- CLAUDE.md) and does not execute anything. Never raises: a snapshot that is not
    vector's, is truncated, or is not JSON degrades to ``None`` and the caller still emits the section
    heading + caption, so the diagram is never silently absent."""
    if not embed_html or len(embed_html) > _MAX_DIAGRAM_SCAN_CHARS:
        return None
    blob = _find_model_blob(embed_html)
    if blob is None:
        return None
    try:
        # ``parse_constant`` kills JSON's non-standard ``Infinity``/``-Infinity``/``NaN`` at the parser
        # rather than leaving every downstream ``int()`` to survive a float special.
        model = json.loads(blob, parse_constant=lambda _name: None)
    except (ValueError, RecursionError):
        return None
    return model if isinstance(model, dict) else None


def _node_state_label(node: dict) -> str:
    """The node's LAST state ("OWNED", "IMPACT", …) — the final keyframe is what a static frame shows."""
    states = [s for s in _d_list(node.get("states")) if isinstance(s, dict)]
    if not states:
        return ""
    last = max(states, key=lambda s: _d_int(s.get("at")))
    return _d_str(last.get("state") or last.get("label"), 40).upper()


def _node_cell_text(node: dict) -> str:
    lines = [_d_str(node.get("label")) or _d_str(node.get("id"))]
    addr = _d_str(node.get("ip")) or _d_str(node.get("domain"))
    if addr:
        lines.append(addr)
    state = _node_state_label(node)
    if state:
        lines.append(f"[{state}]")
    return "\n".join(x for x in lines if x)


def _add_diagram_grid(doc, model: dict) -> bool:
    """The static frame: zones as columns, ``row`` as rows — the same placement ``vector-viewer.js``'s
    ``geometry()`` computes. Returns False when the model has nothing placeable to draw."""
    zones = [z for z in _d_list(model.get("zones")) if isinstance(z, dict)]
    zones.sort(key=lambda z: _d_int(z.get("order")))
    zones = zones[:_MAX_DIAGRAM_ZONES]
    if not zones:
        return False
    by_zone: dict[str, dict[int, list[dict]]] = {_d_str(z.get("id")): {} for z in zones}
    max_row = 0
    # ``nodes`` is the one list vector's own caps do not reach here, and every node sharing a
    # ``(zone, row)`` is concatenated into ONE cell -- one ``<w:br/>`` element each. Measured
    # uncapped: 100k nodes = 8.8s and 204 MB peak per render, multiplied by however many diagrams are
    # linked. Cap it like the rest.
    for node in _d_list(model.get("nodes"))[:_MAX_DIAGRAM_NODES]:
        if not isinstance(node, dict):
            continue
        zone_id = _d_str(node.get("zone"))
        if zone_id not in by_zone:
            continue  # a node in a zone this table does not show (unknown id, or past the zone cap)
        row = max(0, min(_d_int(node.get("row")), _MAX_DIAGRAM_ROWS - 1))
        by_zone[zone_id].setdefault(row, []).append(node)
        max_row = max(max_row, row)
    if not any(by_zone.values()):
        return False

    table = doc.add_table(rows=1, cols=len(zones))
    table.style = "Table Grid"
    for cell, zone in zip(table.rows[0].cells, zones, strict=True):
        cell.paragraphs[0].add_run(_d_str(zone.get("title")) or _d_str(zone.get("id"))).bold = True
        subtitle = _d_str(zone.get("subtitle"))
        if subtitle:
            cell.add_paragraph(subtitle)
    for row_idx in range(max_row + 1):
        cells = table.add_row().cells
        for cell, zone in zip(cells, zones, strict=True):
            nodes = by_zone[_d_str(zone.get("id"))].get(row_idx, [])
            cell.text = "\n".join(_node_cell_text(n) for n in nodes)
    return True


def _add_diagram_walkthrough(doc, model: dict) -> None:
    """The phase-by-phase narrative the animation steps through — the part of an attack path that a
    still frame genuinely cannot carry, and the part a report reader most needs."""
    phases = [p for p in _d_list(model.get("phases")) if isinstance(p, dict)]
    phases.sort(key=lambda p: _d_int(p.get("n")))
    phases = phases[:_MAX_DIAGRAM_PHASES]
    if not phases:
        return
    doc.add_paragraph("Walkthrough", style="Heading 3")
    for phase in phases:
        title = _d_str(phase.get("title")) or ("Overview" if phase.get("intro") else "")
        head = f"Phase {_d_int(phase.get('n'))}"
        if title:
            head += f" — {title}"
        mitre = _d_str(phase.get("mitre"), 120)
        if mitre:
            head += f"  ({mitre})"
        p = doc.add_paragraph()
        p.add_run(head).bold = True
        desc = _d_str(phase.get("desc"), 1200)
        if desc:
            doc.add_paragraph(desc)


def _add_diagram_edges(doc, model: dict) -> None:
    """The connections, resolved to node LABELS — an edge list of bare ids is not a deliverable."""
    labels = {
        _d_str(n.get("id")): (_d_str(n.get("label")) or _d_str(n.get("id")))
        for n in _d_list(model.get("nodes"))[:_MAX_DIAGRAM_NODES]
        if isinstance(n, dict)
    }
    edges = [e for e in _d_list(model.get("edges")) if isinstance(e, dict)][:_MAX_DIAGRAM_EDGES]
    rendered = []
    for edge in edges:
        src, dst = _d_str(edge.get("from")), _d_str(edge.get("to"))
        if src not in labels or dst not in labels:
            continue  # vector drops dangling edges too — do not draw a line to nowhere
        text = f"{labels[src]} → {labels[dst]}"
        detail = " · ".join(x for x in (_d_str(edge.get("label")), _d_str(edge.get("kind"), 60)) if x)
        rendered.append(f"{text}  ({detail})" if detail else text)
    if not rendered:
        return
    doc.add_paragraph("Connections", style="Heading 3")
    for line in rendered:
        doc.add_paragraph(line, style="List Bullet")


def _append_attack_paths(doc, ctx: ReportContext) -> None:
    """Append the Attack Paths section to the RENDERED document (ext#115), mirroring
    ``_append_checklists``/``_append_evidence_appendix``: programmatic and post-render, no Jinja loop
    authored into the binary template.

    Placed BEFORE the checklists and the evidence appendix so the .docx section order matches the HTML
    templates' (``findings`` -> ``diagrams`` -> ``methodology`` -> ``evidence``, see
    ``reporting/templates.py``) — which is also what makes ``context.number_figures``'s single
    numbering sequence correct for both deliverables.

    Renders nothing when the engagement has no linked diagram, so a report without one is byte-identical
    to before this section existed (the same backward-compat guarantee ``render_html._render_diagrams``
    makes on the HTML side)."""
    if not ctx.diagrams:
        return
    doc.add_heading("Attack Paths", level=1)
    doc.add_paragraph(
        "Static rendition of each interactive attack path delivered with the HTML report: the zones "
        "and hosts as placed in the diagram, and the phase walkthrough, at the final step."
    )
    # Nothing caps how many diagrams may be linked to an engagement (``scribble_link_attack_path``
    # appends unconditionally), so cap what one section renders -- same reasoning as render_html's
    # ``_MAX_APPENDIX_ITEMS``, and SAY SO in the document rather than truncating silently.
    withheld = max(0, len(ctx.diagrams) - _MAX_DIAGRAMS)
    if withheld:
        note = doc.add_paragraph()
        note.add_run(
            f"{withheld} further diagram{'s' if withheld != 1 else ''} linked to this engagement "
            f"{'are' if withheld != 1 else 'is'} not shown here "
            f"(this section lists at most {_MAX_DIAGRAMS})."
        ).italic = True
    for index, d in enumerate(ctx.diagrams[:_MAX_DIAGRAMS], start=1):
        model = _diagram_model(d.embed_html)
        meta = model.get("meta") if isinstance(model, dict) else None
        # ``d.caption`` comes from the link route, which strips NUL but not the other 22 XML-illegal
        # control characters -- and those reach lxml unfiltered through add_heading/add_run.
        title = _xml_safe(d.caption or "") or _d_str((meta or {}).get("title")) or f"Attack path {index}"
        doc.add_heading(title, level=2)
        subtitle = _d_str((meta or {}).get("subtitle"))
        if subtitle:
            doc.add_paragraph(subtitle)
        drew_grid = _add_diagram_grid(doc, model) if model else False
        if model:
            _add_diagram_walkthrough(doc, model)
            _add_diagram_edges(doc, model)
        if not model or not drew_grid:
            # Be loud about a snapshot this renderer could not read: an honest "the diagram is over
            # there" beats the silent omission ext#115 is about.
            note = doc.add_paragraph()
            note.add_run(
                "This diagram is delivered as an interactive figure in the HTML report; a static "
                "rendition of its layout is not available here."
            ).italic = True
        # The caption goes BENEATH the figure, carrying the same continuous number the HTML prints.
        caption = doc.add_paragraph()
        caption.add_run(figure_caption(d.figure_number, title)).italic = True


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
        caption = _numbered_caption(a)  # ext#117
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
    # Section order mirrors the HTML templates' block order (findings -> diagrams -> methodology ->
    # evidence, reporting/templates.py), which is what makes context.number_figures' single figure
    # sequence come out the same in both deliverables.
    _append_attack_paths(tpl.docx, ctx)  # ext#115
    _append_checklists(tpl.docx, ctx)  # programmatic, post-render (no Jinja in the binary template)
    _append_evidence_appendix(tpl.docx, ctx, artifact_bytes=artifact_bytes)

    buf = io.BytesIO()
    tpl.save(buf)
    return buf.getvalue()
