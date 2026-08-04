"""Minimal, self-contained document renderer — CREAM's own lightweight deliverable pipeline (no
dependency on Fraction's report renderer).

``render_document_html`` produces a clean HTML invoice/quote (header + line-item table + totals). PDF is
optional: if ``weasyprint`` is installed, ``render_document_pdf`` renders bytes; otherwise callers serve
the HTML. Keeping PDF optional avoids forcing a heavy native dependency on every lotek install.
"""

from __future__ import annotations

from html import escape

from cream.models import Document
from cream.service import totals

_CSS = """
body{font-family:system-ui,-apple-system,'Segoe UI',Roboto,sans-serif;color:#111;margin:2rem;max-width:52rem}
h1{font-size:1.4rem;margin:0 0 .25rem} .muted{color:#666;font-size:.85rem}
table{width:100%;border-collapse:collapse;margin:1.25rem 0}
th,td{text-align:left;padding:.5rem .4rem;border-bottom:1px solid #ddd}
td.num,th.num{text-align:right} tfoot td{font-weight:700;border-top:2px solid #333;border-bottom:none}
.badge{display:inline-block;padding:.1rem .5rem;border-radius:.4rem;background:#eef;font-size:.75rem}
"""


def _rows_html(doc: Document) -> str:
    out = []
    for li in doc.line_items:
        out.append(
            f"<tr><td>{escape(li.description)}</td>"
            f"<td class='num'>{float(li.qty):g}</td>"
            f"<td class='num'>{float(li.unit_price):,.2f}</td>"
            f"<td class='num'>{li.amount:,.2f}</td></tr>"
        )
    return "".join(out) or "<tr><td colspan='4' class='muted'>No line items yet.</td></tr>"


def render_document_html(doc: Document, *, standalone: bool = False) -> str:
    t = totals(doc)
    cur = escape(doc.currency)
    num = (" " + escape(doc.number)) if doc.number else ""
    notes = ('<p class="muted">' + escape(doc.notes) + "</p>") if doc.notes else ""
    head = (
        "<thead><tr><th>Description</th><th class='num'>Qty</th>"
        f"<th class='num'>Unit ({cur})</th><th class='num'>Amount ({cur})</th></tr></thead>"
    )
    total_row = (
        f"<tfoot><tr><td colspan='3' class='num'>Total ({cur})</td>"
        f"<td class='num'>{t['total']:,.2f}</td></tr></tfoot>"
    )
    body = f"""
<h1>{escape(doc.kind.value.title())}{num}</h1>
<div class="muted">{escape(doc.title)} · <span class="badge">{escape(doc.status.value)}</span></div>
<table>
  {head}
  <tbody>{_rows_html(doc)}</tbody>
  {total_row}
</table>
{notes}
"""
    if not standalone:
        return body
    return f"<!doctype html><html><head><meta charset='utf-8'><style>{_CSS}</style>" \
           f"<title>{escape(doc.kind.value.title())} {escape(doc.number or '')}</title></head>" \
           f"<body>{body}</body></html>"


def render_document_pdf(doc: Document) -> bytes | None:
    """PDF bytes via weasyprint if available, else None (caller falls back to HTML)."""
    try:
        from weasyprint import HTML  # type: ignore  # optional heavy native dep
    except Exception:  # noqa: BLE001 - optional dependency; absence is expected, not an error
        return None
    return HTML(string=render_document_html(doc, standalone=True)).write_pdf()
