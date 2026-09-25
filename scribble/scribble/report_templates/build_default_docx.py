"""Authors ``scribble/report_templates/default.docx`` — the docxtpl template the docx/PDF report renders
against. Run to (re)generate the committed binary after a layout change::

    uv run python -m scribble.report_templates.build_default_docx

It only *authors* the template: every ``{{ }}`` / ``{% %}`` string is inert literal text until
``reporting/render_docx.py`` loads the ``.docx`` as a ``docxtpl.DocxTemplate`` and calls ``.render()``.

Design (the shipped, dogfooded starter — operators may upload their own later):

1. **Cover** — an accent band with the engagement name, then company / client / scope / dates / generated
   / a CONFIDENTIAL marker. Page break.
2. **Table of Contents** — a Word TOC field. Page numbers are computed by the PDF engine (Gotenberg's
   ``updateIndexes``, and Word/LibreOffice on open, since ``w:updateFields`` is set). Page break.
3. **Executive Summary** — overall-risk line (color driven by a Jinja if/elif over ``rollup.overall``),
   a one-line count, the generated narrative, and a severity-count table with per-severity shaded cells.
4. **Findings** — ``{% for group %}`` / ``{% for f %}``: finding title (Heading 2), a severity/CVSS/target
   meta table (severity cell shaded via ``{% cellbg %}``), status, the rich body (``{{r f.body }}``), and
   evidence. A page footer carries the page number on every page.

Fonts are restricted to families present in the PDF-render image (Liberation/DejaVu) so the PDF is not
tofu. Colors track ``render_html.py``'s palette so HTML and PDF read the same.
"""
from __future__ import annotations

from pathlib import Path

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Pt, RGBColor

OUTPUT_PATH = Path(__file__).resolve().parent / "default.docx"

BODY_FONT = "Liberation Sans"       # Arial-metric; shipped in the Gotenberg image (fonts-liberation2)
ACCENT = "0F7A52"                   # app --accent green
ACCENT_DK = "0A5B3D"
INK = "131B24"
INK2 = "3D4B59"
MUTED = "6B7A89"
LINE = "CBD4DD"
WHITE = "FFFFFF"

SEVERITY_COLORS = {
    "critical": "B91C1C", "high": "DC2626", "medium": "EA580C", "low": "CA8A04", "info": "0284C7",
}
SEVERITY_ORDER = ("critical", "high", "medium", "low", "info")


# ----------------------------------------------------------------------------- low-level docx helpers

def _shade(el, hex_color: str) -> None:
    """Fill a cell or paragraph (its ``w:tcPr``/``w:pPr``) with a solid color."""
    pr = el.get_or_add_tcPr() if el.tag.endswith("}tc") else el.get_or_add_pPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), hex_color)
    pr.append(shd)


def _bottom_rule(paragraph, hex_color: str = ACCENT, size: int = 8) -> None:
    """An accent rule beneath a paragraph (a bottom border) — the section-heading underline."""
    pPr = paragraph._p.get_or_add_pPr()
    borders = OxmlElement("w:pBdr")
    bottom = OxmlElement("w:bottom")
    bottom.set(qn("w:val"), "single")
    bottom.set(qn("w:sz"), str(size))
    bottom.set(qn("w:space"), "2")
    bottom.set(qn("w:color"), hex_color)
    borders.append(bottom)
    pPr.append(borders)


def _field(paragraph, instr: str, placeholder: str = "") -> None:
    """A Word complex field computed by the renderer (TOC, PAGE) — begin/instr/separate/ph/end."""
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


def _spacing(paragraph, *, before: float = 0, after: float = 6, line: float | None = None) -> None:
    paragraph.paragraph_format.space_before = Pt(before)
    paragraph.paragraph_format.space_after = Pt(after)
    if line is not None:
        paragraph.paragraph_format.line_spacing = line


# ----------------------------------------------------------------------------- styles / chrome

def _set_styles(doc: Document) -> None:
    def font(style_name, *, name=BODY_FONT, size, color, bold=False):
        st = doc.styles[style_name]
        st.font.name = name
        st.font.size = Pt(size)
        st.font.color.rgb = RGBColor.from_string(color)
        st.font.bold = bold
        # bind the latin font for the East-Asian slot too, so it actually takes.
        rpr = st.element.get_or_add_rPr()
        rfonts = rpr.find(qn("w:rFonts")) or OxmlElement("w:rFonts")
        rfonts.set(qn("w:ascii"), name)
        rfonts.set(qn("w:hAnsi"), name)
        if rpr.find(qn("w:rFonts")) is None:
            rpr.append(rfonts)

    font("Normal", size=10.5, color=INK)
    doc.styles["Normal"].paragraph_format.space_after = Pt(6)
    doc.styles["Normal"].paragraph_format.line_spacing = 1.12
    font("Title", size=26, color=INK, bold=True)
    font("Heading 1", size=17, color=ACCENT_DK, bold=True)
    font("Heading 2", size=13, color=INK, bold=True)
    font("Heading 3", size=11.5, color=INK2, bold=True)
    font("Heading 4", size=10.5, color=MUTED, bold=True)


def _set_updatefields(doc: Document) -> None:
    settings = doc.settings.element
    if settings.find(qn("w:updateFields")) is None:
        uf = OxmlElement("w:updateFields")
        uf.set(qn("w:val"), "true")
        settings.append(uf)


def _add_page_footer(doc: Document) -> None:
    footer = doc.sections[0].footer
    footer.is_linked_to_previous = False
    p = footer.paragraphs[0] if footer.paragraphs else footer.add_paragraph()
    p.text = ""
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    r = p.add_run("Page ")
    r.font.size = Pt(8)
    r.font.color.rgb = RGBColor.from_string(MUTED)
    _field(p, "PAGE", "1")
    r2 = p.add_run(" of ")
    r2.font.size = Pt(8)
    r2.font.color.rgb = RGBColor.from_string(MUTED)
    _field(p, "NUMPAGES", "1")


def _heading(doc: Document, text_markup: str, *, rule: bool = True) -> None:
    p = doc.add_paragraph(text_markup, style="Heading 1")
    _spacing(p, before=4, after=6)
    if rule:
        _bottom_rule(p)


# ----------------------------------------------------------------------------- sections

def _add_cover(doc: Document) -> None:
    # Accent band across the page carrying the engagement name (white on green).
    band = doc.add_table(rows=1, cols=1)
    band.autofit = True
    cell = band.rows[0].cells[0]
    _shade(cell.tcPr if hasattr(cell, "tcPr") else cell._tc, ACCENT)
    _shade(cell._tc, ACCENT)
    cp = cell.paragraphs[0]
    _spacing(cp, before=10, after=10)
    run = cp.add_run("{{ engagement_name }}")
    run.bold = True
    run.font.size = Pt(24)
    run.font.color.rgb = RGBColor.from_string(WHITE)
    run.font.name = BODY_FONT

    def meta(label: str, value_markup: str) -> None:
        p = doc.add_paragraph()
        _spacing(p, before=2, after=2)
        lab = p.add_run(label + "  ")
        lab.bold = True
        lab.font.color.rgb = RGBColor.from_string(MUTED)
        val = p.add_run(value_markup)
        val.font.color.rgb = RGBColor.from_string(INK)

    doc.add_paragraph()
    meta("Client", "{{ client_name }}")
    meta("Company", "{{ company_name }}")
    meta("Scope", "{{ scope_type }}")
    meta("Assessment window", "{{ start_date }} – {{ end_date }}")
    meta("Report generated", "{{ generated_date }}")

    doc.add_paragraph()
    conf = doc.add_paragraph()
    conf.alignment = WD_ALIGN_PARAGRAPH.CENTER
    cr = conf.add_run("CONFIDENTIAL")
    cr.bold = True
    cr.font.size = Pt(12)
    cr.font.color.rgb = RGBColor.from_string("B91C1C")
    doc.add_page_break()


def _add_toc(doc: Document) -> None:
    _heading(doc, "Table of Contents")
    p = doc.add_paragraph()
    _field(p, 'TOC \\o "1-3" \\h \\z \\u', "The table of contents is generated when the report is produced.")
    doc.add_page_break()


def _conditional_colored_run(paragraph, var: str, value_markup: str) -> None:
    keys = list(SEVERITY_ORDER)
    for i, key in enumerate(keys):
        kw = "if" if i == 0 else "elif"
        paragraph.add_run(f'{{% {kw} {var} == "{key}" %}}')
        run = paragraph.add_run(value_markup)
        run.bold = True
        run.font.color.rgb = RGBColor.from_string(SEVERITY_COLORS[key])
    paragraph.add_run("{% else %}")
    run = paragraph.add_run(value_markup)
    run.bold = True
    run.font.color.rgb = RGBColor.from_string(MUTED)
    paragraph.add_run("{% endif %}")


def _add_executive_summary(doc: Document) -> None:
    _heading(doc, "Executive Summary")

    risk_p = doc.add_paragraph()
    risk_p.add_run("Overall risk: ").bold = True
    _conditional_colored_run(risk_p, "rollup.overall", "{{ rollup.overall_label }}")

    count_p = doc.add_paragraph()
    count_p.add_run("{{ groups|length }} section(s), {{ rollup.total }} finding(s) in this report.")

    narrative_p = doc.add_paragraph()
    narrative_p.add_run("{{ narrative }}")

    table = doc.add_table(rows=2, cols=len(SEVERITY_ORDER) + 1)
    table.style = "Table Grid"
    hdr = table.rows[0].cells
    dat = table.rows[1].cells
    for i, sev in enumerate(SEVERITY_ORDER):
        _shade(hdr[i]._tc, SEVERITY_COLORS[sev])
        hr = hdr[i].paragraphs[0].add_run(sev.title())
        hr.bold = True
        hr.font.color.rgb = RGBColor.from_string(WHITE)
        hdr[i].paragraphs[0].alignment = WD_ALIGN_PARAGRAPH.CENTER
        dp = dat[i].paragraphs[0]
        dp.alignment = WD_ALIGN_PARAGRAPH.CENTER
        dp.add_run(f"{{{{ rollup.counts.{sev} }}}}").bold = True
    _shade(hdr[-1]._tc, INK)
    tr = hdr[-1].paragraphs[0].add_run("Total")
    tr.bold = True
    tr.font.color.rgb = RGBColor.from_string(WHITE)
    hdr[-1].paragraphs[0].alignment = WD_ALIGN_PARAGRAPH.CENTER
    dat[-1].paragraphs[0].alignment = WD_ALIGN_PARAGRAPH.CENTER
    dat[-1].paragraphs[0].add_run("{{ rollup.total }}").bold = True
    doc.add_page_break()


def _severity_color_expr(var: str) -> str:
    expr = f'"{SEVERITY_COLORS[SEVERITY_ORDER[-1]]}"'
    for sev in reversed(SEVERITY_ORDER[:-1]):
        expr = f'("{SEVERITY_COLORS[sev]}" if {var} == "{sev}" else {expr})'
    return expr


def _add_finding_meta_table(doc: Document) -> None:
    table = doc.add_table(rows=1, cols=3)
    table.style = "Table Grid"
    sev_cell, cvss_cell, target_cell = table.rows[0].cells

    sev_p = sev_cell.paragraphs[0]
    sev_p.add_run(f"{{% cellbg {_severity_color_expr('f.severity')} %}}")
    sev_run = sev_p.add_run("{{ f.severity_label }}")
    sev_run.bold = True
    sev_run.font.color.rgb = RGBColor.from_string(WHITE)

    cvss_p = cvss_cell.paragraphs[0]
    cvss_p.add_run("{% if f.cvss_score %}")
    cvss_p.add_run("CVSS {{ f.cvss_score }}")
    cvss_p.add_run("{% endif %}")

    target_p = target_cell.paragraphs[0]
    target_p.add_run("{% if f.target %}")
    target_p.add_run("{{ f.target }}")
    target_p.add_run("{% endif %}")


def _add_evidence_section(doc: Document) -> None:
    doc.add_paragraph("{% if f.artifacts %}")
    doc.add_paragraph("Evidence", style="Heading 4")
    doc.add_paragraph("{% for a in f.artifacts %}")
    doc.add_paragraph("{% if a.embedded %}")
    doc.add_paragraph().add_run("{{ a.image }}")
    doc.add_paragraph().add_run("{{ a.caption }}").italic = True
    doc.add_paragraph("{% else %}")
    doc.add_paragraph().add_run("\U0001f4c4 {{ a.filename }} — {{ a.caption }} (not embedded)").italic = True
    doc.add_paragraph("{% endif %}")
    doc.add_paragraph("{% endfor %}")
    doc.add_paragraph("{% endif %}")


def _add_findings_body(doc: Document) -> None:
    _heading(doc, "Findings")
    doc.add_paragraph("{% if not groups %}")
    doc.add_paragraph("No findings recorded for this engagement.")
    doc.add_paragraph("{% endif %}")

    doc.add_paragraph("{% for group in groups %}")
    gp = doc.add_paragraph("{{ group.name }}", style="Heading 2")
    _spacing(gp, before=8, after=4)
    doc.add_paragraph("{% if not group.findings %}")
    doc.add_paragraph("No findings in this section.")
    doc.add_paragraph("{% endif %}")

    doc.add_paragraph("{% for f in group.findings %}")
    fp = doc.add_paragraph("{{ f.title }}", style="Heading 3")
    _spacing(fp, before=8, after=2)
    _add_finding_meta_table(doc)
    doc.add_paragraph("{% if f.status_label %}")
    status_p = doc.add_paragraph()
    status_p.add_run("Status: ").italic = True
    status_p.add_run("{{ f.status_label }}").bold = True
    doc.add_paragraph("{% endif %}")
    doc.add_paragraph().add_run("{{r f.body }}")
    _add_evidence_section(doc)
    doc.add_paragraph("{% endfor %}")
    doc.add_paragraph("{% endfor %}")


def build() -> Document:
    doc = Document()
    _set_styles(doc)
    _set_updatefields(doc)
    _add_page_footer(doc)
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
