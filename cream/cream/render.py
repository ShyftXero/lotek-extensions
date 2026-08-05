"""Self-contained document renderer — CREAM's own lightweight deliverable pipeline (no dependency on
Scribble's report renderer).

One function renders every surface: the live preview pane, the standalone HTML export, and the PDF. They
take the same :class:`~cream.viewmodel.DocumentView` through the same code, so a preview cannot show
something the PDF will not — the failure mode that makes an editor untrustworthy.

Everything interpolated here is either escaped (:mod:`cream.markup`) or validated against a pattern. Two
inputs get specific attention because they are not merely display strings:

* **the logo** must be a ``data:`` image URI. The PDF engine fetches whatever a document references, so
  an ``http(s)`` logo would make "render this invoice" an outbound request from the server — an SSRF
  primitive handed to anyone who can edit branding.
* **accent colour and font stack** land inside a ``<style>`` block, where an unvalidated value could
  close the element and inject markup. Both are pattern-matched, and anything unrecognised falls back to
  the default rather than being escaped-and-passed-through.

PDF stays optional: with ``weasyprint`` installed :func:`render_document_pdf` returns bytes, otherwise
``None`` and the caller serves HTML. It is a heavy native dependency and most installs never print.
"""

from __future__ import annotations

import re
from html import escape

from cream.markup import plain, render_markup
from cream.money import fmt
from cream.viewmodel import DocumentView

_DEFAULT_ACCENT = "#0f766e"
_DEFAULT_FONT = "system-ui,-apple-system,'Segoe UI',Roboto,sans-serif"

_COLOR_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$|^[a-zA-Z]{3,20}$")
_FONT_RE = re.compile(r"^[\w \-,'\"]{1,255}$")
#: Raster formats only — same allowlist as ``api._clean_logo``. SVG is excluded on purpose.
_LOGO_PREFIX_RE = re.compile(r"^data:image/(png|jpeg|jpg|gif|webp);base64,", re.IGNORECASE)


def safe_color(value: str | None) -> str:
    """A hex triplet or a bare CSS colour keyword; anything else -> the default accent."""
    candidate = (value or "").strip()
    return candidate if _COLOR_RE.match(candidate) else _DEFAULT_ACCENT


def safe_font(value: str | None) -> str:
    """A font-family list of word/quote/comma characters only; anything else -> the default stack."""
    candidate = (value or "").strip()
    return candidate if _FONT_RE.match(candidate) else _DEFAULT_FONT


def safe_logo(value: str | None) -> str | None:
    """Only an inline **raster** ``data:image/...`` URI survives. A remote URL is dropped, not fetched.

    The allowlist matches ``api._clean_logo`` deliberately. An earlier version of this function accepted
    any ``data:image/*``, which made the second layer weaker than the first — ``image/svg+xml`` would
    have passed here even though the API refuses to store it, and SVG is a document format with its own
    fetching and scripting surface. Defence in depth is only depth if the inner layer is not the looser
    one; the two are now equivalent, and either alone is sufficient.
    """
    candidate = (value or "").strip()
    if not _LOGO_PREFIX_RE.match(candidate):
        return None
    if any(ch in candidate for ch in ('"', "'", "<", ">", "\n", "\r", " ")):
        return None  # a data URI is base64/percent-encoded; these characters mean somebody is trying
    return candidate if len(candidate) <= 2_000_000 else None


def _css(view: DocumentView) -> str:
    accent = safe_color(view.issuer.accent_color)
    font = safe_font(view.issuer.font_stack)
    return f"""
:root {{ --accent: {accent}; --ink: #14202b; --muted: #64748b; --rule: #e2e8f0; }}
.cream-doc {{ font-family: {font}; color: var(--ink); font-size: 13px; line-height: 1.5;
  background: #fff; }}
.cream-doc * {{ box-sizing: border-box; }}
.cream-doc h1 {{ font-size: 26px; margin: 0; letter-spacing: .02em; color: var(--accent); }}
.cream-doc h2 {{ font-size: 13px; margin: 26px 0 8px; text-transform: uppercase;
  letter-spacing: .09em; color: var(--muted); border-bottom: 1px solid var(--rule);
  padding-bottom: 5px; }}
.cream-doc .muted {{ color: var(--muted); }}
.cream-doc .top {{ display: flex; justify-content: space-between; gap: 28px;
  align-items: flex-start; }}
.cream-doc .logo {{ max-height: 68px; max-width: 240px; margin-bottom: 10px; }}
.cream-doc .issuer {{ font-size: 12px; }}
.cream-doc .issuer .name {{ font-weight: 700; font-size: 15px; color: var(--ink); }}
.cream-doc .docmeta {{ text-align: right; min-width: 210px; }}
.cream-doc .docmeta .num {{ font-family: ui-monospace, Menlo, Consolas, monospace;
  font-size: 14px; font-weight: 700; }}
.cream-doc .status {{ display: inline-block; padding: 2px 9px; border-radius: 999px;
  background: var(--accent); color: #fff; font-size: 10px; text-transform: uppercase;
  letter-spacing: .1em; }}
.cream-doc .status.draft {{ background: #b45309; }}
.cream-doc .status.void {{ background: #b91c1c; }}
.cream-doc .parties {{ display: flex; gap: 28px; margin: 26px 0 4px; }}
.cream-doc .parties > div {{ flex: 1; }}
.cream-doc .kv {{ width: 100%; font-size: 12px; }}
.cream-doc .kv td {{ padding: 2px 0; vertical-align: top; }}
.cream-doc .kv td:first-child {{ color: var(--muted); padding-right: 12px; white-space: nowrap; }}
.cream-doc table.lines {{ width: 100%; border-collapse: collapse; margin: 8px 0 0; }}
.cream-doc table.lines th {{ text-align: left; font-size: 10px; text-transform: uppercase;
  letter-spacing: .09em; color: var(--muted); border-bottom: 2px solid var(--accent);
  padding: 7px 6px; }}
.cream-doc table.lines td {{ padding: 9px 6px; border-bottom: 1px solid var(--rule);
  vertical-align: top; }}
.cream-doc .num-col {{ text-align: right; white-space: nowrap; }}
.cream-doc .detail {{ color: var(--muted); font-size: 11.5px; margin-top: 4px; }}
.cream-doc .detail p {{ margin: 3px 0; }}
.cream-doc .detail ul {{ margin: 3px 0; padding-left: 17px; }}
.cream-doc code {{ font-family: ui-monospace, Menlo, Consolas, monospace; font-size: .93em;
  background: #f1f5f9; padding: 0 3px; border-radius: 3px; }}
.cream-doc .totals {{ margin-left: auto; margin-top: 14px; width: 300px; font-size: 12.5px; }}
.cream-doc .totals td {{ padding: 4px 6px; }}
.cream-doc .totals td:last-child {{ text-align: right; white-space: nowrap; }}
.cream-doc .totals tr.grand td {{ border-top: 2px solid var(--accent); font-weight: 700;
  font-size: 15px; padding-top: 8px; }}
.cream-doc .scope {{ columns: 2; column-gap: 26px; font-family: ui-monospace, Menlo, Consolas,
  monospace; font-size: 11.5px; margin: 0; padding-left: 17px; }}
.cream-doc .sigbox {{ margin-top: 22px; display: flex; gap: 28px; }}
.cream-doc .sigline {{ flex: 1; border-bottom: 1px solid var(--ink); height: 40px; }}
.cream-doc .siglabel {{ font-size: 10.5px; color: var(--muted); margin-top: 4px; }}
.cream-doc .foot {{ margin-top: 30px; padding-top: 12px; border-top: 1px solid var(--rule);
  font-size: 11px; color: var(--muted); }}
@media print {{ .cream-doc {{ font-size: 11.5px; }} }}
@page {{ size: letter; margin: 16mm 14mm; }}
"""


def _issuer_block(view: DocumentView) -> str:
    issuer = view.issuer
    logo = safe_logo(issuer.logo_data_uri)
    bits = [f'<img class="logo" src="{escape(logo, quote=True)}" alt="">'] if logo else []
    bits.append(f'<div class="name">{plain(issuer.company_name) or "—"}</div>')
    if issuer.address:
        bits.append(f'<div class="muted">{render_markup(issuer.address)}</div>')
    contact = " · ".join(plain(x) for x in (issuer.email, issuer.phone, issuer.website) if x)
    if contact:
        bits.append(f'<div class="muted">{contact}</div>')
    if issuer.tax_id:
        bits.append(f'<div class="muted">Tax ID {plain(issuer.tax_id)}</div>')
    return f'<div class="issuer">{"".join(bits)}</div>'


def _meta_block(view: DocumentView) -> str:
    rows: list[tuple[str, str]] = []
    if view.issued_at:
        rows.append(("Issued", plain(view.issued_at)))
    if view.valid_until:
        rows.append(("Valid until", plain(view.valid_until)))
    if view.due_date:
        rows.append(("Payment due", plain(view.due_date)))
    if view.window_display:
        rows.append(("Execution window", plain(view.window_display)))
    if view.reference:
        rows.append(("Your reference", plain(view.reference)))
    if not rows:
        return ""
    cells = "".join(f"<tr><td>{k}</td><td>{v}</td></tr>" for k, v in rows)
    return f'<table class="kv">{cells}</table>'


def _bill_to_block(view: DocumentView) -> str:
    if not any((view.bill_to_name, view.bill_to_address, view.bill_to_email, view.bill_to_attn)):
        return ""
    bits = ["<h2>Bill to</h2>"]
    if view.bill_to_name:
        bits.append(f"<div><strong>{plain(view.bill_to_name)}</strong></div>")
    if view.bill_to_attn:
        bits.append(f'<div class="muted">Attn: {plain(view.bill_to_attn)}</div>')
    if view.bill_to_address:
        bits.append(f'<div class="muted">{render_markup(view.bill_to_address)}</div>')
    if view.bill_to_email:
        bits.append(f'<div class="muted">{plain(view.bill_to_email)}</div>')
    return "".join(bits)


def _lines_block(view: DocumentView) -> str:
    cur = escape(view.currency or "")
    head = (
        "<thead><tr><th>Description</th><th class='num-col'>Qty</th>"
        f"<th class='num-col'>Unit price</th><th class='num-col'>Amount ({cur})</th></tr></thead>"
    )
    rows = []
    for line in view.lines:
        detail = render_markup(line.detail)
        detail_html = f'<div class="detail">{detail}</div>' if detail else ""
        rows.append(
            "<tr>"
            f"<td>{plain(line.description)}{detail_html}</td>"
            f"<td class='num-col'>{plain(line.qty_display)}</td>"
            f"<td class='num-col'>{fmt(line.unit_price)}</td>"
            f"<td class='num-col'>{fmt(line.amount)}</td>"
            "</tr>"
        )
    body = "".join(rows) or "<tr><td colspan='4' class='muted'>No line items yet.</td></tr>"
    return f'<table class="lines">{head}<tbody>{body}</tbody></table>'


def _totals_block(view: DocumentView) -> str:
    tot = view.totals
    cur = escape(view.currency or "")
    rows = [f"<tr><td class='muted'>Subtotal</td><td>{fmt(tot.subtotal)} {cur}</td></tr>"]
    if tot.discount:
        rows.append(
            f"<tr><td class='muted'>{plain(tot.discount_label)}</td>"
            f"<td>−{fmt(tot.discount)} {cur}</td></tr>"
        )
    if tot.tax or tot.tax_label:
        label = plain(tot.tax_label) or "Tax"
        rows.append(f"<tr><td class='muted'>{label}</td><td>{fmt(tot.tax)} {cur}</td></tr>")
    rows.append(f"<tr class='grand'><td>Total</td><td>{fmt(tot.total)} {cur}</td></tr>")
    return f'<table class="totals">{"".join(rows)}</table>'


def _scope_block(view: DocumentView) -> str:
    if not view.scope:
        return ""
    items = "".join(f"<li>{plain(target)}</li>" for target in view.scope)
    return (
        "<h2>Appendix A — Scope of testing</h2>"
        '<p class="muted">Testing is authorized against the following targets only, and only within the '
        "execution window stated above.</p>"
        f'<ul class="scope">{items}</ul>'
    )


def _authorization_block(view: DocumentView) -> str:
    if not view.authorization_required:
        return ""
    terms = render_markup(view.roe_terms)
    signatory = plain(view.signatory_name)
    title = plain(view.signatory_title)
    who = f"{signatory}{', ' + title if title else ''}" if signatory else "Authorized signatory"
    return (
        "<h2>Authorization to test</h2>"
        f"{terms or ''}"
        "<p>By signing below, the undersigned confirms authority to permit security testing of the "
        "targets listed in Appendix A during the stated execution window.</p>"
        '<div class="sigbox">'
        f'<div><div class="sigline"></div><div class="siglabel">{who}</div></div>'
        '<div style="max-width:170px"><div class="sigline"></div>'
        '<div class="siglabel">Date</div></div>'
        "</div>"
    )


def _footer_block(view: DocumentView) -> str:
    bits = []
    if view.notes:
        bits.append(f"<h2>Notes</h2>{render_markup(view.notes)}")
    if view.issuer.payment_instructions:
        bits.append(f"<h2>Payment</h2>{render_markup(view.issuer.payment_instructions)}")
    if view.issuer.footer_terms:
        bits.append(f'<div class="foot">{render_markup(view.issuer.footer_terms)}</div>')
    return "".join(bits)


def render_document_html(view: DocumentView, *, standalone: bool = False) -> str:
    """The document as HTML. ``standalone`` wraps it in a full page with its own ``<style>``; otherwise
    a fragment is returned for embedding (the preview pane and the in-app viewer)."""
    status_class = escape(view.status if view.status in ("draft", "void") else "")
    heading = escape((view.kind or "document").title())
    number = f'<div class="num">{plain(view.number)}</div>' if view.number else ""
    body = f"""
<div class="cream-doc">
  <div class="top">
    {_issuer_block(view)}
    <div class="docmeta">
      <h1>{heading}</h1>
      {number}
      <div style="margin:6px 0 10px"><span class="status {status_class}">{escape(view.status)}</span></div>
      {_meta_block(view)}
    </div>
  </div>
  <div class="parties">
    <div>{_bill_to_block(view)}</div>
    <div><h2>Engagement</h2><div><strong>{plain(view.title) or "—"}</strong></div></div>
  </div>
  <h2>Services</h2>
  {_lines_block(view)}
  {_totals_block(view)}
  {_scope_block(view)}
  {_authorization_block(view)}
  {_footer_block(view)}
</div>
"""
    if not standalone:
        return f"<style>{_css(view)}</style>{body}"
    title = escape(f"{heading} {view.number or ''}".strip())
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>{title}</title><style>{_css(view)}"
        "body{margin:0;padding:26px;background:#fff}</style></head>"
        f"<body>{body}</body></html>"
    )


def render_document_pdf(view: DocumentView) -> bytes | None:
    """PDF bytes via weasyprint if available, else ``None`` (caller falls back to HTML).

    ``base_url`` is deliberately **not** passed: without it weasyprint has no document root to resolve a
    relative reference against, so the only images that can load are the inline ``data:`` URIs
    :func:`safe_logo` already vetted.
    """
    try:
        from weasyprint import HTML  # type: ignore  # optional heavy native dep
    except Exception:  # noqa: BLE001 - optional dependency; absence is expected, not an error
        return None
    return HTML(string=render_document_html(view, standalone=True)).write_pdf()
