"""Authors ``scribble/report_templates/default.docx`` — the docxtpl template the docx/PDF report renders
against. Run to (re)generate the committed binary after a layout change::

    uv run python -m scribble.report_templates.build_default_docx

It only *authors* the template: every ``{{ }}`` / ``{% %}`` string is inert literal text until
``reporting/render_docx.py`` loads the ``.docx`` as a ``docxtpl.DocxTemplate`` and calls ``.render()``.

Design goal: read like the HTML deliverable — narrow margins, findings as bordered **cards** with a
severity-colored left bar, green small-caps section labels, a grey monospace code block, and the print
severity ramp. Colors/shapes track ``render_html.py``'s print CSS so the two deliverables match. Fonts
are restricted to families present in the PDF-render (Gotenberg) image so the PDF is not tofu.

Structure: cover → Table of Contents (field) → Executive Summary (colored severity table) →
Findings (one card per finding: severity bar + title + meta + rich body + evidence). Page footer "Page N
of M" throughout. ``w:updateFields`` is set so the TOC/page numbers recompute at render.
"""
from __future__ import annotations

import re
from pathlib import Path

from docx import Document
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement, parse_xml
from docx.oxml.ns import qn
from docx.shared import Inches, Pt, RGBColor

from scribble.reporting.fonts import DEFAULT_BODY_FONT as BODY_FONT
from scribble.reporting.fonts import DEFAULT_CODE_FONT as MONO_FONT

OUTPUT_PATH = Path(__file__).resolve().parent / "default.docx"

# Report typography (BODY_FONT / MONO_FONT are imported at the top from reporting.fonts): Inter for
# body/headings, JetBrains Mono for code — both baked into the lotek-gotenberg image
# (extensions/scribble/gotenberg), so the PDF renders them instead of substituting a stock face. Sourced
# from reporting.fonts so the render-time font remap replaces exactly what the template baked in.
# Palette = render_html.py's PRINT (light) CSS, so HTML and PDF read the same.
ACCENT = "0F7A52"
ACCENT_INK = "0A5B3D"
ACCENT_WASH = "E7F3ED"
INK = "131B24"
INK2 = "3D4B59"
MUTED = "6B7A89"
LINE = "E3E8ED"
SURFACE2 = "EFF3F6"
WHITE = "FFFFFF"
SEVERITY_COLORS = {  # print ramp
    "critical": "B3261E", "high": "C2410C", "medium": "A16207", "low": "1D6FA5", "info": "64748B",
}
SEVERITY_ORDER = ("critical", "high", "medium", "low", "info")


# ----------------------------------------------------------------------------- oxml helpers

def _shade(tc_or_p, hex_color: str) -> None:
    pr = tc_or_p.get_or_add_tcPr() if tc_or_p.tag.endswith("}tc") else tc_or_p.get_or_add_pPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), hex_color)
    pr.append(shd)


def _cell_borders(cell, *, color=LINE, sz=6, sides=("top", "left", "bottom", "right")) -> None:
    tcPr = cell._tc.get_or_add_tcPr()
    borders = OxmlElement("w:tcBorders")
    for side in sides:
        el = OxmlElement(f"w:{side}")
        el.set(qn("w:val"), "single")
        el.set(qn("w:sz"), str(sz))
        el.set(qn("w:space"), "0")
        el.set(qn("w:color"), color)
        borders.append(el)
    tcPr.append(borders)


def _no_cell_borders(cell) -> None:
    tcPr = cell._tc.get_or_add_tcPr()
    borders = OxmlElement("w:tcBorders")
    for side in ("top", "left", "bottom", "right"):
        el = OxmlElement(f"w:{side}")
        el.set(qn("w:val"), "nil")
        borders.append(el)
    tcPr.append(borders)


def _cell_margins(cell, *, top=40, bottom=40, left=100, right=100) -> None:
    tcPr = cell._tc.get_or_add_tcPr()
    m = OxmlElement("w:tcMar")
    for side, val in (("top", top), ("bottom", bottom), ("left", left), ("right", right)):
        el = OxmlElement(f"w:{side}")
        el.set(qn("w:w"), str(val))
        el.set(qn("w:type"), "dxa")
        m.append(el)
    tcPr.append(m)


def _fixed_col_widths(table, widths_in: list[float]) -> None:
    """Force column widths (LibreOffice/Word ignore ``cell.width`` without a fixed table layout + a
    matching ``tblGrid``)."""
    table.autofit = False
    table.allow_autofit = False
    tblPr = table._tbl.tblPr
    layout = OxmlElement("w:tblLayout")
    layout.set(qn("w:type"), "fixed")
    tblPr.append(layout)
    total = sum(widths_in)
    w = OxmlElement("w:tblW")
    w.set(qn("w:w"), str(int(total * 1440)))
    w.set(qn("w:type"), "dxa")
    tblPr.append(w)
    grid = table._tbl.find(qn("w:tblGrid"))
    if grid is not None:
        table._tbl.remove(grid)
    grid = OxmlElement("w:tblGrid")
    for width in widths_in:
        col = OxmlElement("w:gridCol")
        col.set(qn("w:w"), str(int(width * 1440)))
        grid.append(col)
    table._tbl.insert(list(table._tbl).index(tblPr) + 1, grid)
    for row in table.rows:
        for cell, width in zip(row.cells, widths_in, strict=False):
            cell.width = Inches(width)


def _field(paragraph, instr: str, placeholder: str = "") -> None:
    run = paragraph.add_run()
    for tag, attrs, text in (
        ("w:fldChar", {"w:fldCharType": "begin"}, None),
        ("w:instrText", {"xml:space": "preserve"}, instr),
        ("w:fldChar", {"w:fldCharType": "separate"}, None),
        ("w:t", {}, placeholder),
        ("w:fldChar", {"w:fldCharType": "end"}, None),
    ):
        el = OxmlElement(tag)
        for k, v in attrs.items():
            el.set(qn(k), v)
        if text is not None:
            el.text = text
        run._r.append(el)


def _spacing(paragraph, *, before=0.0, after=4.0, line=None) -> None:
    paragraph.paragraph_format.space_before = Pt(before)
    paragraph.paragraph_format.space_after = Pt(after)
    if line is not None:
        paragraph.paragraph_format.line_spacing = line


def _run(paragraph, text, *, size=10.5, color=INK, bold=False, italic=False, mono=False, caps=False):
    r = paragraph.add_run(text)
    r.font.name = MONO_FONT if mono else BODY_FONT
    r.font.size = Pt(size)
    r.font.color.rgb = RGBColor.from_string(color)
    r.bold = bold
    r.italic = italic
    if caps:
        rpr = r._r.get_or_add_rPr()
        caps_el = OxmlElement("w:caps")
        caps_el.set(qn("w:val"), "true")
        rpr.append(caps_el)
        sp = OxmlElement("w:spacing")   # letter-spacing
        sp.set(qn("w:val"), "8")
        rpr.append(sp)
    return r


def _chip(paragraph, text, *, fill, color, size=8, caps=False):
    """A filled chip/badge — bold text on a character-shaded fill, hair-space padded so the fill reads as
    a chip. SQUARE (run shading can't round); kept for non-pill uses. Prefer :func:`_pill` for the card
    badges — verified to render rounded through LibreOffice."""
    r = _run(paragraph, f" {text} ", size=size, color=color, bold=True, caps=caps)
    rpr = r._r.get_or_add_rPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), fill)
    rpr.append(shd)
    return r


_W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_PILL_ID = [1000]

# Invisible section markers — an empty paragraph carrying a uniquely-named Word bookmark
# ``scribble-section:<key>`` — let render_docx._reorder_sections find where each report section begins and
# re-emit the sections in the operator's per-report order (ReportBoard.section_order). The marker survives
# docxtpl render (it has no Jinja) and the marker paragraphs are removed during the reorder, so they never
# appear in the delivered document. ``key`` is a reporting.layouts.BLOCK_KEYS value.
SECTION_MARKER_PREFIX = "scribble-section:"
_BOOKMARK_ID = [5000]


def add_section_marker(container, key: str):
    """Append an invisible ``scribble-section:<key>`` bookmark paragraph to ``container`` (a Document or a
    cell). See :data:`SECTION_MARKER_PREFIX`."""
    p = container.add_paragraph()
    _spacing(p, before=0, after=0)
    _BOOKMARK_ID[0] += 1
    bid = str(_BOOKMARK_ID[0])
    start = OxmlElement("w:bookmarkStart")
    start.set(qn("w:id"), bid)
    start.set(qn("w:name"), f"{SECTION_MARKER_PREFIX}{key}")
    end = OxmlElement("w:bookmarkEnd")
    end.set(qn("w:id"), bid)
    p._p.append(start)
    p._p.append(end)
    return p


def _visible_len(text: str) -> int:
    """Rendered length of a label that may carry a Jinja expression: a ``{{ … }}`` fills to a short value
    at render, so estimate it at 2 chars (a count like ``{{ f.assets|length }}``) rather than measuring the
    long template source — otherwise a fixed-width chip is sized for the markup, not the text."""
    return len(re.sub(r"\{\{.*?\}\}", "00", text))


def label_chip_xml(text: str, *, width_in: float | None = None) -> str:
    """A ROUNDED, padded section-label chip: an inline DrawingML ``roundRect`` (fully rounded) holding
    left-aligned bold green small-caps text on a soft green wash — the rounded, padded form of the old flat
    green label, so the labels match the header pills and the rounded code box.

    Labels get this treatment and the PROSE blocks under them do NOT, on purpose: a DrawingML shape can't
    break across pages, so a long Description/Remediation would clip — a label is short and fixed, so it is
    shape-safe. ``text`` may contain Jinja ({{ }}), filled by docxtpl at render; width is fixed (a shape
    can't autosize to a variable), estimated from the visible length when not given. Namespaces are declared
    inline so it renders whether appended to a paragraph or embedded in a RichText body."""
    _PILL_ID[0] += 1
    did = _PILL_ID[0]
    if width_in is None:
        width_in = min(2.7, max(0.95, 0.085 * _visible_len(text) + 0.24))
    cx, cy = int(width_in * 914400), int(0.24 * 914400)
    return (
        f'<w:r xmlns:w="{_W_NS}"><w:drawing>'
        '<wp:inline xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"'
        ' distT="0" distB="0" distL="0" distR="0">'
        f'<wp:extent cx="{cx}" cy="{cy}"/><wp:effectExtent l="0" t="0" r="0" b="0"/>'
        f'<wp:docPr id="{did}" name="label{did}"/>'
        '<a:graphic xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
        '<a:graphicData uri="http://schemas.microsoft.com/office/word/2010/wordprocessingShape">'
        '<wps:wsp xmlns:wps="http://schemas.microsoft.com/office/word/2010/wordprocessingShape">'
        '<wps:cNvSpPr txBox="0"/>'
        f'<wps:spPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="{cx}" cy="{cy}"/></a:xfrm>'
        '<a:prstGeom prst="roundRect"><a:avLst><a:gd name="adj" fmla="val 50000"/></a:avLst></a:prstGeom>'
        f'<a:solidFill><a:srgbClr val="{ACCENT_WASH}"/></a:solidFill><a:ln><a:noFill/></a:ln></wps:spPr>'
        '<wps:txbx><w:txbxContent><w:p><w:pPr><w:jc w:val="left"/>'
        '<w:spacing w:before="0" w:after="0" w:line="240" w:lineRule="auto"/></w:pPr>'
        '<w:r><w:rPr><w:b/><w:caps w:val="true"/><w:spacing w:val="8"/>'
        f'<w:color w:val="{ACCENT_INK}"/><w:sz w:val="15"/>'
        f'<w:rFonts w:ascii="{BODY_FONT}" w:hAnsi="{BODY_FONT}"/></w:rPr>'
        f'<w:t xml:space="preserve">{text}</w:t></w:r></w:p></w:txbxContent></wps:txbx>'
        '<wps:bodyPr rot="0" anchor="ctr" anchorCtr="0" lIns="64008" tIns="0" rIns="45720" bIns="0"/>'
        '</wps:wsp></a:graphicData></a:graphic></wp:inline></w:drawing></w:r>'
    )


def _section_label(paragraph, text):
    """The card's rounded section label (Reproduction / Affected Assets / Evidence) — a rounded, padded
    green chip (:func:`label_chip_xml`). Sibling to render_docx's body labels, which share the same chip."""
    _spacing(paragraph, before=6, after=3)
    paragraph._p.append(parse_xml(label_chip_xml(text)))


def _pill(paragraph, text_markup, *, fill, color, width_in, caps=False, size=15):
    """A ROUNDED pill badge: an inline DrawingML ``roundRect`` shape (fully rounded, ``adj=50000``) with
    centered bold text. Verified to render rounded through Gotenberg/LibreOffice — the HTML card's pill,
    in the docx. ``text_markup`` may contain Jinja ({{ f.severity_label }}), filled by docxtpl at render.
    Width is FIXED (a shape can't autosize to a template variable), so each caller sizes generously for its
    longest content and the centered text pads out; height is a fixed 0.24\"."""
    _PILL_ID[0] += 1
    did = _PILL_ID[0]
    cx, cy = int(width_in * 914400), int(0.24 * 914400)
    caps_xml = '<w:caps w:val="true"/>' if caps else ""
    xml = (
        f'<w:r xmlns:w="{_W_NS}"><w:drawing>'
        '<wp:inline xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"'
        ' distT="0" distB="0" distL="0" distR="0">'
        f'<wp:extent cx="{cx}" cy="{cy}"/><wp:effectExtent l="0" t="0" r="0" b="0"/>'
        f'<wp:docPr id="{did}" name="pill{did}"/>'
        '<a:graphic xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
        '<a:graphicData uri="http://schemas.microsoft.com/office/word/2010/wordprocessingShape">'
        '<wps:wsp xmlns:wps="http://schemas.microsoft.com/office/word/2010/wordprocessingShape">'
        '<wps:cNvSpPr txBox="0"/>'
        f'<wps:spPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="{cx}" cy="{cy}"/></a:xfrm>'
        '<a:prstGeom prst="roundRect"><a:avLst><a:gd name="adj" fmla="val 50000"/></a:avLst></a:prstGeom>'
        f'<a:solidFill><a:srgbClr val="{fill}"/></a:solidFill><a:ln><a:noFill/></a:ln></wps:spPr>'
        '<wps:txbx><w:txbxContent><w:p><w:pPr><w:jc w:val="center"/>'
        '<w:spacing w:before="0" w:after="0" w:line="240" w:lineRule="auto"/></w:pPr>'
        f'<w:r><w:rPr><w:b/>{caps_xml}<w:color w:val="{color}"/><w:sz w:val="{size}"/>'
        f'<w:rFonts w:ascii="{BODY_FONT}" w:hAnsi="{BODY_FONT}"/></w:rPr>'
        f'<w:t xml:space="preserve">{text_markup}</w:t></w:r></w:p></w:txbxContent></wps:txbx>'
        '<wps:bodyPr rot="0" anchor="ctr" anchorCtr="1" lIns="45720" tIns="0" rIns="45720" bIns="0"/>'
        '</wps:wsp></a:graphicData></a:graphic></wp:inline></w:drawing></w:r>'
    )
    paragraph._p.append(parse_xml(xml))


def _code_box(paragraph, loop_expr, item_var, *, width_in=6.85, fill=SURFACE2, color=INK2):
    """A ROUNDED, padded code box: a roundRect shape (gentle 8% radius) whose txbxContent runs a Jinja
    ``{%p for %}`` loop over ``loop_expr``, one monospace line per item ({{ item_var }}). Auto-grows to
    the line count (spAutoFit). Verified end-to-end through docxtpl + LibreOffice — real internal padding
    and rounded corners, unlike a shaded paragraph."""
    _PILL_ID[0] += 1
    did = _PILL_ID[0]
    cx, cy = int(width_in * 914400), int(0.3 * 914400)
    inner = (
        '<w:p><w:pPr><w:spacing w:before="0" w:after="0"/></w:pPr>'
        '<w:r><w:t xml:space="preserve">{%p for ' + item_var + ' in ' + loop_expr + ' %}</w:t></w:r></w:p>'
        '<w:p><w:pPr><w:spacing w:before="0" w:after="0" w:line="240" w:lineRule="auto"/></w:pPr>'
        '<w:r><w:rPr><w:rFonts w:ascii="' + MONO_FONT + '" w:hAnsi="' + MONO_FONT + '"/>'
        '<w:color w:val="' + color + '"/><w:sz w:val="17"/></w:rPr>'
        '<w:t xml:space="preserve">{{ ' + item_var + ' }}</w:t></w:r></w:p>'
        '<w:p><w:pPr><w:spacing w:before="0" w:after="0"/></w:pPr>'
        '<w:r><w:t xml:space="preserve">{%p endfor %}</w:t></w:r></w:p>'
    )
    xml = (
        f'<w:r xmlns:w="{_W_NS}"><w:drawing>'
        '<wp:inline xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"'
        ' distT="0" distB="0" distL="0" distR="0">'
        f'<wp:extent cx="{cx}" cy="{cy}"/><wp:effectExtent l="0" t="0" r="0" b="0"/>'
        f'<wp:docPr id="{did}" name="codebox{did}"/>'
        '<a:graphic xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
        '<a:graphicData uri="http://schemas.microsoft.com/office/word/2010/wordprocessingShape">'
        '<wps:wsp xmlns:wps="http://schemas.microsoft.com/office/word/2010/wordprocessingShape">'
        '<wps:cNvSpPr txBox="1"/>'
        f'<wps:spPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="{cx}" cy="{cy}"/></a:xfrm>'
        '<a:prstGeom prst="roundRect"><a:avLst><a:gd name="adj" fmla="val 8000"/></a:avLst></a:prstGeom>'
        f'<a:solidFill><a:srgbClr val="{fill}"/></a:solidFill><a:ln><a:noFill/></a:ln></wps:spPr>'
        f'<wps:txbx><w:txbxContent>{inner}</w:txbxContent></wps:txbx>'
        '<wps:bodyPr rot="0" anchor="t" lIns="91440" tIns="54864" rIns="91440" bIns="54864">'
        '<a:spAutoFit/></wps:bodyPr>'
        '</wps:wsp></a:graphicData></a:graphic></wp:inline></w:drawing></w:r>'
    )
    paragraph._p.append(parse_xml(xml))


# ----------------------------------------------------------------------------- doc chrome

def _set_styles(doc: Document) -> None:
    def style(name, *, fname=BODY_FONT, size, color, bold=False):
        st = doc.styles[name]
        st.font.size = Pt(size)
        st.font.color.rgb = RGBColor.from_string(color)
        st.font.bold = bold
        # Set the font on the style's EXISTING rFonts in place, stripping the theme attrs — Word/LibreOffice
        # honor a ``*Theme`` reference over a plain ``ascii``, and an appended second rFonts is ignored, so
        # appending mono to "No Spacing" left its theme font (a serif under Gotenberg) winning.
        rpr = st.element.get_or_add_rPr()
        rf = rpr.find(qn("w:rFonts"))
        if rf is None:
            rf = OxmlElement("w:rFonts")
            rpr.insert(0, rf)
        for attr in ("w:asciiTheme", "w:hAnsiTheme", "w:eastAsiaTheme", "w:cstheme"):
            if rf.get(qn(attr)) is not None:
                del rf.attrib[qn(attr)]
        rf.set(qn("w:ascii"), fname)
        rf.set(qn("w:hAnsi"), fname)
        rf.set(qn("w:cs"), fname)

    # Document-wide default font: RichText body runs (the finding write-ups) carry no explicit font, so
    # without this LibreOffice falls back to a serif and the body clashes with the sans chrome.
    rpr_default = doc.styles.element.find(qn("w:docDefaults"))
    if rpr_default is not None:
        rpr = rpr_default.find(qn("w:rPrDefault"))
        if rpr is not None:
            r = rpr.find(qn("w:rPr"))
            if r is None:
                r = OxmlElement("w:rPr")
                rpr.append(r)
            # Replace the existing (theme) rFonts in place — Word/LibreOffice honor the FIRST rFonts, so
            # appending a second is silently ignored and the theme font (an unresolvable minorHAnsi →
            # Liberation Serif under Gotenberg) wins.
            rf = r.find(qn("w:rFonts"))
            if rf is None:
                rf = OxmlElement("w:rFonts")
                r.insert(0, rf)
            for attr in ("w:asciiTheme", "w:hAnsiTheme", "w:eastAsiaTheme", "w:cstheme"):
                if rf.get(qn(attr)) is not None:
                    del rf.attrib[qn(attr)]
            rf.set(qn("w:ascii"), BODY_FONT)
            rf.set(qn("w:hAnsi"), BODY_FONT)
            rf.set(qn("w:cs"), BODY_FONT)

    style("Normal", size=10, color=INK)
    doc.styles["Normal"].paragraph_format.space_after = Pt(5)
    doc.styles["Normal"].paragraph_format.line_spacing = 1.12
    style("Title", size=26, color=INK, bold=True)
    style("Heading 1", size=16, color=ACCENT_INK, bold=True)
    style("Heading 2", size=12.5, color=INK, bold=True)
    style("Heading 3", size=11, color=INK, bold=True)
    style("Heading 4", size=9, color=ACCENT_INK, bold=True)

    # A content code block (a ``<pre>`` in a Description/Details block) renders under the "No Spacing"
    # paragraph style (content/render_docx._CODE_BLOCK_STYLE). Style it as a MONO, shaded code box so it
    # matches the Reproduction box instead of rendering as serif prose — a `curl` PoC or an HTTP request
    # in Details reads as code, not paragraph text.
    style("No Spacing", fname=MONO_FONT, size=8.5, color=INK2)
    _code = doc.styles["No Spacing"]
    _code.paragraph_format.space_before = Pt(2)
    _code.paragraph_format.space_after = Pt(6)
    _code_shd = OxmlElement("w:shd")
    _code_shd.set(qn("w:val"), "clear")
    _code_shd.set(qn("w:color"), "auto")
    _code_shd.set(qn("w:fill"), SURFACE2)
    _code.element.get_or_add_pPr().append(_code_shd)


def _set_margins(doc: Document) -> None:
    # Tight but not cramped. 0.1" read as goofy — the content ran to the paper edge. ~0.6" keeps the
    # dense, modern feel while leaving a proper print gutter; the cards' own borders + padding do the
    # rest of the framing.
    for s in doc.sections:
        s.left_margin = Inches(0.6)
        s.right_margin = Inches(0.6)
        s.top_margin = Inches(0.5)
        s.bottom_margin = Inches(0.5)    # room for the footer
        s.footer_distance = Inches(0.25)


def _set_updatefields(doc: Document) -> None:
    settings = doc.settings.element
    if settings.find(qn("w:updateFields")) is None:
        uf = OxmlElement("w:updateFields")
        uf.set(qn("w:val"), "true")
        settings.append(uf)


def _add_footer(doc: Document) -> None:
    footer = doc.sections[0].footer
    footer.is_linked_to_previous = False
    p = footer.paragraphs[0] if footer.paragraphs else footer.add_paragraph()
    p.text = ""
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    _run(p, "Page ", size=8, color=MUTED)
    _field(p, "PAGE", "1")
    _run(p, " of ", size=8, color=MUTED)
    _field(p, "NUMPAGES", "1")


def _heading1(doc: Document, text: str):
    p = doc.add_paragraph(text, style="Heading 1")
    _spacing(p, before=2, after=6)
    pPr = p._p.get_or_add_pPr()
    bdr = OxmlElement("w:pBdr")
    bottom = OxmlElement("w:bottom")
    bottom.set(qn("w:val"), "single")
    bottom.set(qn("w:sz"), "8")
    bottom.set(qn("w:space"), "2")
    bottom.set(qn("w:color"), ACCENT)
    bdr.append(bottom)
    pPr.append(bdr)
    return p


# ----------------------------------------------------------------------------- sections

def _add_cover(doc: Document) -> None:
    # Cover logo (the picked library logo or the stock lotek mark) — a docxtpl InlineImage filled by
    # render_docx; an empty context value renders nothing (a report that somehow has no logo just opens on
    # the title band).
    logo_p = doc.add_paragraph()
    logo_p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    _spacing(logo_p, before=0, after=8)
    logo_p.add_run("{{ cover_logo }}")

    band = doc.add_table(rows=1, cols=1)
    band.alignment = WD_TABLE_ALIGNMENT.CENTER
    cell = band.rows[0].cells[0]
    _shade(cell._tc, ACCENT)
    _cell_margins(cell, top=180, bottom=180, left=160, right=160)
    _no_cell_borders(cell)
    cp = cell.paragraphs[0]
    _run(cp, "{{ engagement_name }}", size=24, color=WHITE, bold=True)
    sub = cell.add_paragraph()
    _run(sub, "Security Assessment Report", size=12, color="D6EFE3")

    doc.add_paragraph()

    def meta(label, value_markup):
        p = doc.add_paragraph()
        _spacing(p, before=1, after=1)
        _run(p, label + "   ", size=10, color=MUTED, bold=True)
        _run(p, value_markup, size=10, color=INK)

    meta("Client", "{{ client_name }}")
    meta("Company", "{{ company_name }}")
    meta("Scope", "{{ scope_type }}")
    meta("Assessment window", "{{ start_date }} – {{ end_date }}")
    meta("Report generated", "{{ generated_date }}")

    doc.add_paragraph()
    conf = doc.add_paragraph()
    conf.alignment = WD_ALIGN_PARAGRAPH.CENTER
    _run(conf, "CONFIDENTIAL", size=11, color=SEVERITY_COLORS["critical"], bold=True, caps=True)
    doc.add_page_break()


def _add_toc(doc: Document) -> None:
    _heading1(doc, "Table of Contents")
    p = doc.add_paragraph()
    _field(p, 'TOC \\o "1-3" \\h \\z \\u', "The table of contents is generated when the report is produced.")
    doc.add_page_break()


def _sev_expr(var: str) -> str:
    expr = f'"{SEVERITY_COLORS[SEVERITY_ORDER[-1]]}"'
    for sev in reversed(SEVERITY_ORDER[:-1]):
        expr = f'("{SEVERITY_COLORS[sev]}" if {var} == "{sev}" else {expr})'
    return expr


def _add_executive_summary(doc: Document) -> None:
    _heading1(doc, "Executive Summary")

    # Punchy verdict — a small "OVERALL RISK" label over a BIG, severity-colored risk word, then a
    # dot-separated count line. Fast to read at a glance, the way the HTML exec summary opens.
    lbl = doc.add_paragraph()
    _spacing(lbl, before=2, after=0)
    _run(lbl, "Overall Risk", size=9, color=MUTED, bold=True, caps=True)
    verdict = doc.add_paragraph()
    _spacing(verdict, before=0, after=1)
    for i, sev in enumerate(SEVERITY_ORDER):
        verdict.add_run(f'{{% {"if" if i == 0 else "elif"} rollup.overall == "{sev}" %}}')
        _run(verdict, "{{ rollup.overall_label }}", size=24, color=SEVERITY_COLORS[sev], bold=True)
    verdict.add_run("{% else %}")
    _run(verdict, "{{ rollup.overall_label }}", size=24, color=MUTED, bold=True)
    verdict.add_run("{% endif %}")
    stat = doc.add_paragraph()
    _spacing(stat, before=0, after=8)
    _run(stat, "{{ rollup.counts.critical }} critical   ·   {{ rollup.counts.high }} high   ·   "
               "{{ rollup.counts.medium }} medium   ·   {{ rollup.counts.low }} low   ·   "
               "{{ rollup.counts.info }} info", size=11, color=INK2, bold=True)

    _run(doc.add_paragraph(), "{{ narrative }}", color=INK2)
    _run(doc.add_paragraph(),
         "{{ groups|length }} section(s), {{ rollup.total }} finding(s) in this report.",
         size=9, color=MUTED)

    # Scope and limitations — the operator's per-report override or the standing statement (resolved to a
    # line list in render_docx._build_context, #Q4). Parity with the HTML front matter, which the .docx was
    # missing. Rendered as paragraphs so both a bullet-style standing list and free prose read cleanly.
    _section_label(doc.add_paragraph(), "Scope and Limitations")
    doc.add_paragraph("{%p for line in scope_limitations %}")
    _run(doc.add_paragraph(), "{{ line }}", size=10, color=INK2)
    doc.add_paragraph("{%p endfor %}")

    table = doc.add_table(rows=2, cols=len(SEVERITY_ORDER) + 1)
    table.alignment = WD_TABLE_ALIGNMENT.LEFT

    def _hcell(cell, text, fill):
        _shade(cell._tc, fill)
        _cell_borders(cell)
        p = cell.paragraphs[0]
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        _run(p, text, size=9, color=WHITE, bold=True)

    def _dcell(cell, markup):
        _cell_borders(cell)
        p = cell.paragraphs[0]
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        _run(p, markup, bold=True)

    hdr, dat = table.rows[0].cells, table.rows[1].cells
    for i, sev in enumerate(SEVERITY_ORDER):
        _hcell(hdr[i], sev.title(), SEVERITY_COLORS[sev])
        _dcell(dat[i], f"{{{{ rollup.counts.{sev} }}}}")
    _hcell(hdr[-1], "Total", INK)
    _dcell(dat[-1], "{{ rollup.total }}")
    doc.add_page_break()


def _finding_card(doc: Document) -> None:
    """One finding as a bordered card: a thin severity-colored left bar + a content cell (title, meta,
    rich body, evidence). Wrapped by the ``{% for f %}`` loop, so each finding gets its own card."""
    table = doc.add_table(rows=1, cols=2)
    table.alignment = WD_TABLE_ALIGNMENT.LEFT
    _fixed_col_widths(table, [0.08, 7.12])  # thin severity bar + content, fits the 0.6" page margins
    bar, content = table.rows[0].cells

    # left bar = severity color, no text.
    bar_p = bar.paragraphs[0]
    bar_p.add_run(f"{{% cellbg {_sev_expr('f.severity')} %}}")
    _cell_borders(bar, color=LINE, sides=("top", "left", "bottom"))
    _cell_margins(bar, left=0, right=0, top=0, bottom=0)

    _cell_borders(content, color=LINE, sides=("top", "right", "bottom"))
    _cell_margins(content, left=160, right=160, top=110, bottom=110)

    # title + meta line. The title is Heading 3 so it lands in the TOC (one entry per vulnerability),
    # with a direct run override for the card's larger size.
    tp = content.paragraphs[0]
    tp.style = doc.styles["Heading 3"]
    _spacing(tp, before=0, after=2)
    _run(tp, "{{ f.title }}", size=13, color=INK, bold=True)

    # Chip row: a filled severity badge + a CVSS chip + an affected-count chip, as ROUNDED pills
    # (mirrors the HTML card's badge row). The full target URL is DELIBERATELY not here — it crammed the
    # line and is already in Reproduction / Affected Assets; the header stays a clean, scannable row.
    # Per-severity fill: one pill variant per severity, wrapped in a Jinja if/elif so only the match renders.
    meta = content.add_paragraph()
    _spacing(meta, before=2, after=5)
    for i, sev in enumerate(SEVERITY_ORDER):
        meta.add_run(f'{{% {"if" if i == 0 else "elif"} f.severity == "{sev}" %}}')
        _pill(meta, "{{ f.severity_label }}", fill=SEVERITY_COLORS[sev], color=WHITE,
              caps=True, width_in=0.95)
    meta.add_run("{% else %}")
    _pill(meta, "{{ f.severity_label }}", fill=MUTED, color=WHITE, caps=True, width_in=0.95)
    meta.add_run("{% endif %}")
    meta.add_run("{% if f.cvss_score %}")
    _run(meta, " ", size=8)
    _pill(meta, "CVSS {{ f.cvss_score }}", fill=SURFACE2, color=INK2, width_in=0.9)
    meta.add_run("{% endif %}")
    meta.add_run("{% if f.assets %}")
    _run(meta, " ", size=8)
    _pill(meta, "{{ f.assets|length }} affected asset{{ 's' if f.assets|length != 1 else '' }}",
          fill=SURFACE2, color=MUTED, width_in=1.7)
    meta.add_run("{% endif %}")

    content.add_paragraph("{%p if f.status_label %}")
    sp = content.add_paragraph()
    _run(sp, "Status: ", size=9, color=MUTED, italic=True)
    _run(sp, "{{ f.status_label }}", size=9, color=INK, bold=True)
    content.add_paragraph("{%p endif %}")

    body_p = content.add_paragraph()
    body_p.add_run("{{r f.body }}")

    # Reproduction — the scanner's request(s), in a ROUNDED, padded monospace code box (one line per
    # distinct pattern; the box auto-grows to the line count).
    content.add_paragraph("{%p if f.repro %}")
    _section_label(content.add_paragraph(), "Reproduction")
    _code_box(content.add_paragraph(), "f.repro", "r")
    content.add_paragraph("{%p endif %}")

    # Affected Assets — deduped services (host:port/proto) as a 3-column monospace GRID (f.asset_rows,
    # each a space-padded fixed-width row), so a 50-host fleet vuln is a compact block, not a full page.
    content.add_paragraph("{%p if f.assets %}")
    _section_label(content.add_paragraph(), "Affected Assets ({{ f.assets|length }})")
    content.add_paragraph("{%p for row in f.asset_rows %}")
    ap = content.add_paragraph()
    _spacing(ap, before=0, after=0)
    _run(ap, "{{ row }}", size=9, color=INK, mono=True)
    content.add_paragraph("{%p endfor %}")
    content.add_paragraph("{%p endif %}")

    # evidence
    content.add_paragraph("{%p if f.artifacts %}")
    _section_label(content.add_paragraph(), "Evidence")
    content.add_paragraph("{%p for a in f.artifacts %}")
    content.add_paragraph("{%p if a.embedded %}")
    img_p = content.add_paragraph()
    img_p.alignment = WD_ALIGN_PARAGRAPH.CENTER   # screenshots centered in the card
    _spacing(img_p, before=4, after=1)
    img_p.add_run("{{ a.image }}")
    cap_p = content.add_paragraph()
    cap_p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    _run(cap_p, "{{ a.caption }}", size=8, color=MUTED, italic=True)
    content.add_paragraph("{%p else %}")
    _run(content.add_paragraph(), "\U0001f4c4 {{ a.filename }} — {{ a.caption }} (not embedded)",
         size=8, color=MUTED, italic=True)
    content.add_paragraph("{%p endif %}")
    content.add_paragraph("{%p endfor %}")
    content.add_paragraph("{%p endif %}")


def _add_findings_body(doc: Document) -> None:
    _heading1(doc, "Findings")
    doc.add_paragraph("{%p if not groups %}")
    doc.add_paragraph("No findings recorded for this engagement.")
    doc.add_paragraph("{%p endif %}")

    doc.add_paragraph("{%p for group in groups %}")
    gp = doc.add_paragraph("{{ group.name }}", style="Heading 2")
    _spacing(gp, before=8, after=4)
    doc.add_paragraph("{%p if not group.findings %}")
    doc.add_paragraph("No findings in this section.")
    doc.add_paragraph("{%p endif %}")

    doc.add_paragraph("{%p for f in group.findings %}")
    _finding_card(doc)
    doc.add_paragraph()  # spacer between cards
    doc.add_paragraph("{%p endfor %}")
    doc.add_paragraph("{%p endfor %}")


def build() -> Document:
    doc = Document()
    _set_styles(doc)
    _set_margins(doc)
    _set_updatefields(doc)
    _add_footer(doc)
    # Each template-authored section is preceded by its invisible section marker, so the post-render
    # reorder (render_docx._reorder_sections) can move/drop it like any programmatically-appended section.
    add_section_marker(doc, "cover")
    _add_cover(doc)
    add_section_marker(doc, "toc")
    _add_toc(doc)
    add_section_marker(doc, "summary")
    _add_executive_summary(doc)
    add_section_marker(doc, "findings")
    _add_findings_body(doc)
    return doc


def main() -> None:
    doc = build()
    doc.save(str(OUTPUT_PATH))
    print(f"wrote {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
