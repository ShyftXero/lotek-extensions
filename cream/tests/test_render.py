"""Rendering: the document actually looks like a document, and hostile input stays inert."""

from __future__ import annotations

from decimal import Decimal

import pytest

from cream.render import render_document_html, safe_color, safe_font, safe_logo
from cream.viewmodel import DocumentView, IssuerView, LineView, TotalsView

_PNG = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUg=="


def _view(**kw) -> DocumentView:
    view = DocumentView(
        kind="invoice", status="issued", number="INV-2026-0001", title="External assessment",
        currency="USD",
        issuer=IssuerView(company_name="Redteam Ltd", address="1 Fake St\nSpringfield",
                          email="billing@redteam.test", tax_id="EIN 12-3456789"),
        bill_to_name="Acme Corp", bill_to_address="9 Client Way", bill_to_email="ap@acme.test",
        issued_at="Oct 12, 2026", window_start="Oct 12, 2026", window_end="Oct 23, 2026",
        lines=[LineView(description="Web application assessment", qty=Decimal("16"), unit="hr",
                        unit_price=Decimal("250.00"))],
        totals=TotalsView(subtotal=Decimal("4000.00"), taxable=Decimal("4000.00"),
                          total=Decimal("4000.00")),
    )
    for key, value in kw.items():
        setattr(view, key, value)
    return view


# --- the document has the parts a document has ---------------------------------------------------


def test_both_parties_appear():
    html = render_document_html(_view())
    assert "Redteam Ltd" in html      # issuer — absent entirely before this branch
    assert "Acme Corp" in html        # bill-to
    assert "EIN 12-3456789" in html


def test_meta_and_execution_window_print():
    html = render_document_html(_view())
    assert "INV-2026-0001" in html
    assert "Execution window" in html
    assert "Oct 12, 2026 – Oct 23, 2026" in html


def test_quantity_carries_its_unit():
    assert "16 hr" in render_document_html(_view())


def test_flat_rate_reads_as_one_project():
    view = _view(lines=[LineView(description="Retainer", qty=Decimal("1"), unit="project",
                                 unit_price=Decimal("10000"))])
    assert "1 project" in render_document_html(view)


def test_discount_and_tax_lines_appear_only_when_set():
    plain_html = render_document_html(_view())
    assert "Discount" not in plain_html
    taxed = render_document_html(_view(totals=TotalsView(
        subtotal=Decimal("1000.00"), discount=Decimal("100.00"), discount_label="Repeat client",
        taxable=Decimal("900.00"), tax=Decimal("180.00"), tax_label="VAT 20%",
        total=Decimal("1080.00"))))
    assert "Repeat client" in taxed
    assert "VAT 20%" in taxed
    assert "1,080.00" in taxed


def test_scope_appendix_renders_when_targets_are_present():
    html = render_document_html(_view(scope=["10.0.0.0/24", "app.acme.test"]))
    assert "Appendix A" in html
    assert "10.0.0.0/24" in html


def test_no_appendix_without_scope():
    assert "Appendix A" not in render_document_html(_view())


def test_authorization_block_is_opt_in():
    assert "Authorization to test" not in render_document_html(_view())
    html = render_document_html(_view(authorization_required=True, signatory_name="Dana Reed",
                                      signatory_title="CISO"))
    assert "Authorization to test" in html
    assert "Dana Reed, CISO" in html


def test_standalone_wraps_a_whole_page():
    html = render_document_html(_view(), standalone=True)
    assert html.startswith("<!doctype html>")
    assert "<title>Invoice INV-2026-0001</title>" in html


def test_an_unnumbered_standalone_page_is_titled_by_the_name_it_is_given():
    """The exported page's ``<title>`` is the browser tab AND the PDF's metadata title. Unnumbered it was
    a bare ``Invoice`` — identical for every draft (ext#46 review round 1)."""
    view = _view(status="draft", number=None)
    assert "<title>Invoice</title>" in render_document_html(view, standalone=True)
    named = render_document_html(view, standalone=True, name="draft …b839c91e20")
    assert "<title>Invoice draft …b839c91e20</title>" in named


def test_a_name_titles_the_page_without_printing_itself_on_the_document():
    """The boundary: naming is app-side. An id tail printed where the invoice number goes would read as an
    invoice number on the copy a client receives."""
    named = render_document_html(_view(status="draft", number=None), standalone=True,
                                 name="draft …b839c91e20")
    document = named.split("</head>", 1)[1]
    assert "b839c91e20" not in document
    assert '<div class="num">' not in document
    assert "<h1>Invoice</h1>" in document


def test_a_name_is_ignored_once_the_document_has_a_number():
    """An issued document is titled by its number; ``document_handle`` returns exactly that, so the two
    paths cannot disagree."""
    html = render_document_html(_view(), standalone=True, name="INV-2026-0001")
    assert "<title>Invoice INV-2026-0001</title>" in html


# --- hostile input --------------------------------------------------------------------------------


def test_a_remote_logo_is_dropped_not_fetched():
    """The PDF engine fetches what a document references; an http logo would be an SSRF primitive."""
    for hostile in ("http://169.254.169.254/latest/meta-data/", "https://evil.test/logo.png",
                    "file:///etc/passwd", "//evil.test/x.png"):
        assert safe_logo(hostile) is None
        html = render_document_html(_view(issuer=IssuerView(company_name="X",
                                                            logo_data_uri=hostile)))
        assert hostile not in html


def test_an_inline_image_is_kept():
    assert safe_logo(_PNG) == _PNG
    assert _PNG in render_document_html(_view(issuer=IssuerView(company_name="X",
                                                                logo_data_uri=_PNG)))


def test_a_data_uri_with_quote_breakout_characters_is_dropped():
    assert safe_logo('data:image/png;base64,AAA" onload="alert(1)') is None


def test_the_render_layer_refuses_svg_exactly_like_the_api_layer():
    """Defence in depth is only depth if the inner layer is not the looser one. The API refuses SVG;
    the renderer must too, or the second check is weaker than the first it claims to back up."""
    svg = "data:image/svg+xml;base64,PHN2Zz48L3N2Zz4="
    assert safe_logo(svg) is None
    assert svg not in render_document_html(_view(issuer=IssuerView(company_name="X",
                                                                   logo_data_uri=svg)))


@pytest.mark.parametrize("hostile", ["</style><script>alert(1)</script>", "red;}body{display:none",
                                     "url(http://evil.test/x)"])
def test_accent_colour_cannot_escape_the_style_block(hostile):
    assert safe_color(hostile) == "#0f766e"
    html = render_document_html(_view(issuer=IssuerView(company_name="X", accent_color=hostile)))
    assert "<script" not in html.lower()
    assert "evil.test" not in html


def test_font_stack_is_pattern_matched():
    assert safe_font("Inter, sans-serif") == "Inter, sans-serif"
    assert safe_font("x;}@import url(http://evil.test)") == \
        "system-ui,-apple-system,'Segoe UI',Roboto,sans-serif"


def test_line_description_is_escaped_in_the_table():
    view = _view(lines=[LineView(description="<script>alert(1)</script>", qty=Decimal("1"),
                                 unit="project", unit_price=Decimal("1"))])
    html = render_document_html(view)
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_line_detail_markup_is_rendered_but_inert():
    view = _view(lines=[LineView(description="Web app", detail="- 10.0.0.0/24\n- <b>nope</b>",
                                 qty=Decimal("1"), unit="project", unit_price=Decimal("1"))])
    html = render_document_html(view)
    assert "<li>10.0.0.0/24</li>" in html
    assert "<li>&lt;b&gt;nope&lt;/b&gt;</li>" in html


def test_empty_document_still_renders():
    html = render_document_html(_view(lines=[], totals=TotalsView()))
    assert "No line items yet." in html
