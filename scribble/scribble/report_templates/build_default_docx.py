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

from pathlib import Path

from docx import Document
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Inches, Pt, RGBColor

OUTPUT_PATH = Path(__file__).resolve().parent / "default.docx"

# Report typography. These faces are baked into the lotek-gotenberg image (extensions/scribble/gotenberg),
# so the PDF renders them instead of substituting; stock gotenberg/gotenberg:8 lacks them and would fall
# back to Liberation/DejaVu. Inter (modern, dense-legible) for body/headings, JetBrains Mono for code.
BODY_FONT = "Inter"
MONO_FONT = "JetBrains Mono"
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
    for s in doc.sections:
        s.left_margin = Inches(0.1)
        s.right_margin = Inches(0.1)
        s.top_margin = Inches(0.35)
        s.bottom_margin = Inches(0.45)   # room for the footer
        s.footer_distance = Inches(0.18)


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

    risk_p = doc.add_paragraph()
    _run(risk_p, "Overall risk: ", bold=True)
    for i, sev in enumerate(SEVERITY_ORDER):
        risk_p.add_run(f'{{% {"if" if i == 0 else "elif"} rollup.overall == "{sev}" %}}')
        _run(risk_p, "{{ rollup.overall_label }}", bold=True, color=SEVERITY_COLORS[sev])
    risk_p.add_run("{% else %}")
    _run(risk_p, "{{ rollup.overall_label }}", bold=True, color=MUTED)
    risk_p.add_run("{% endif %}")

    _run(doc.add_paragraph(),
         "{{ groups|length }} section(s), {{ rollup.total }} finding(s) in this report.", color=INK2)
    _run(doc.add_paragraph(), "{{ narrative }}", color=INK2)

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
    _fixed_col_widths(table, [0.09, 8.1])   # thin severity bar + wide content
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

    meta = content.add_paragraph()
    _spacing(meta, after=4)
    for i, sev in enumerate(SEVERITY_ORDER):
        meta.add_run(f'{{% {"if" if i == 0 else "elif"} f.severity == "{sev}" %}}')
        _run(meta, "{{ f.severity_label }}", size=9, color=SEVERITY_COLORS[sev], bold=True, caps=True)
    meta.add_run("{% else %}")
    _run(meta, "{{ f.severity_label }}", size=9, color=MUTED, bold=True, caps=True)
    meta.add_run("{% endif %}")
    meta.add_run("{% if f.cvss_score %}")
    _run(meta, "    CVSS {{ f.cvss_score }}", size=9, color=INK2, bold=True)
    meta.add_run("{% endif %}")
    meta.add_run("{% if f.target %}")
    _run(meta, "    {{ f.target }}", size=9, color=MUTED, mono=True)
    meta.add_run("{% endif %}")

    content.add_paragraph("{%p if f.status_label %}")
    sp = content.add_paragraph()
    _run(sp, "Status: ", size=9, color=MUTED, italic=True)
    _run(sp, "{{ f.status_label }}", size=9, color=INK, bold=True)
    content.add_paragraph("{%p endif %}")

    body_p = content.add_paragraph()
    body_p.add_run("{{r f.body }}")

    # Reproduction — the scanner's request(s), in a shaded monospace code box (one per distinct pattern).
    content.add_paragraph("{%p if f.repro %}")
    _run(content.add_paragraph(), "Reproduction", size=9, color=ACCENT_INK, bold=True, caps=True)
    content.add_paragraph("{%p for r in f.repro %}")
    rp = content.add_paragraph()
    _shade(rp._p, SURFACE2)
    _spacing(rp, before=1, after=1)
    _run(rp, "{{ r }}", size=8.5, color=INK2, mono=True)
    content.add_paragraph("{%p endfor %}")
    content.add_paragraph("{%p endif %}")

    # Affected Assets — deduped services (host:port/proto) as a 3-column monospace GRID (f.asset_rows,
    # each a space-padded fixed-width row), so a 50-host fleet vuln is a compact block, not a full page.
    content.add_paragraph("{%p if f.assets %}")
    _run(content.add_paragraph(), "Affected Assets ({{ f.assets|length }})",
         size=9, color=ACCENT_INK, bold=True, caps=True)
    content.add_paragraph("{%p for row in f.asset_rows %}")
    ap = content.add_paragraph()
    _spacing(ap, before=0, after=0)
    _run(ap, "{{ row }}", size=9, color=INK, mono=True)
    content.add_paragraph("{%p endfor %}")
    content.add_paragraph("{%p endif %}")

    # evidence
    content.add_paragraph("{%p if f.artifacts %}")
    _run(content.add_paragraph(), "Evidence", size=9, color=ACCENT_INK, bold=True, caps=True)
    content.add_paragraph("{%p for a in f.artifacts %}")
    content.add_paragraph("{%p if a.embedded %}")
    content.add_paragraph().add_run("{{ a.image }}")
    _run(content.add_paragraph(), "{{ a.caption }}", size=8, color=MUTED, italic=True)
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
    _add_cover(doc)
    _add_toc(doc)
    _add_executive_summary(doc)
    _add_findings_body(doc)
    return doc


def main() -> None:
    doc = build()
    doc.save(str(OUTPUT_PATH))
    print(f"wrote {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
