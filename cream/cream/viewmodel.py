"""The one shape the renderer consumes — and the reason preview, export, and the frozen copy agree.

Three things need to become a document: a live draft being edited, an unsaved payload in the editor's
preview pane, and the immutable snapshot of an issued invoice. Rendering each of those from its own
source would give three renderers that drift, and the day the preview disagrees with the PDF is the day
nobody trusts the preview. So all three are first flattened into a :class:`DocumentView` and exactly one
renderer walks it.

The snapshot is the sharp part. ``Document.snapshot_json`` is written at issue and an issued document
**renders from it**, not from the live rows — so a firm that changes its letterhead, its rate card, or
its client record next quarter cannot retroactively alter a document already in a client's hands. The
serialized form keeps money as **strings**, not JSON floats: a snapshot that round-trips ``0.1 + 0.2``
through binary floating point is not the record it claims to be.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from decimal import Decimal

from cream.enums import DEFAULT_UNIT
from cream.money import ZERO, money

SNAPSHOT_VERSION = 1


def _fmt_date(value: date | datetime | None) -> str:
    """``Oct 12, 2026``. Built by hand rather than ``%-d``, which is not portable."""
    if value is None:
        return ""
    if isinstance(value, datetime):
        value = value.date()
    return f"{value:%b} {value.day}, {value.year}"


@dataclass
class LineView:
    description: str = ""
    detail: str | None = None
    qty: Decimal = Decimal("1.00")
    unit: str = DEFAULT_UNIT
    unit_price: Decimal = ZERO
    source: str = "manual"

    @property
    def amount(self) -> Decimal:
        return money(money(self.qty) * money(self.unit_price))

    @property
    def qty_display(self) -> str:
        """``16 hr`` / ``1 project``. Trailing zeros dropped — nobody bills ``16.00 hr``."""
        q = money(self.qty).normalize()
        text = format(q, "f") if q == q.to_integral_value() else f"{money(self.qty):.2f}"
        return f"{text} {self.unit}".strip()


@dataclass
class IssuerView:
    company_name: str = ""
    address: str | None = None
    email: str | None = None
    phone: str | None = None
    website: str | None = None
    tax_id: str | None = None
    logo_data_uri: str | None = None
    accent_color: str = "#0f766e"
    font_stack: str = "system-ui,-apple-system,'Segoe UI',Roboto,sans-serif"
    payment_instructions: str | None = None
    footer_terms: str | None = None


@dataclass
class TotalsView:
    subtotal: Decimal = ZERO
    discount: Decimal = ZERO
    discount_label: str = "Discount"
    taxable: Decimal = ZERO
    tax: Decimal = ZERO
    tax_label: str = ""
    total: Decimal = ZERO


@dataclass
class DocumentView:
    """Everything the renderer needs, and nothing it has to go looking up."""

    kind: str = "invoice"
    status: str = "draft"
    number: str | None = None
    title: str = ""
    currency: str = "USD"
    reference: str | None = None

    issuer: IssuerView = field(default_factory=IssuerView)

    bill_to_name: str | None = None
    bill_to_attn: str | None = None
    bill_to_address: str | None = None
    bill_to_email: str | None = None

    issued_at: str = ""
    valid_until: str = ""
    due_date: str = ""
    window_start: str = ""
    window_end: str = ""

    lines: list[LineView] = field(default_factory=list)
    totals: TotalsView = field(default_factory=TotalsView)
    notes: str | None = None

    scope: list[str] = field(default_factory=list)
    authorization_required: bool = False
    signatory_name: str | None = None
    signatory_title: str | None = None
    roe_terms: str | None = None

    @property
    def window_display(self) -> str:
        if self.window_start and self.window_end:
            return f"{self.window_start} – {self.window_end}"
        return self.window_start or self.window_end or ""

    # --- snapshot ------------------------------------------------------------------------------------

    def to_snapshot(self) -> str:
        """Serialize for ``Document.snapshot_json``. Money as strings; ``Decimal`` is not JSON-native and
        ``float`` would quietly change the numbers this document is a record of."""

        def _enc(obj):
            if isinstance(obj, Decimal):
                return str(money(obj))
            raise TypeError(f"not snapshot-serializable: {type(obj).__name__}")

        payload = asdict(self)
        payload["_version"] = SNAPSHOT_VERSION
        return json.dumps(payload, ensure_ascii=False, default=_enc)

    @classmethod
    def from_snapshot(cls, raw: str) -> DocumentView | None:
        """Rebuild from ``snapshot_json``. Returns ``None`` on anything unreadable — a corrupt snapshot
        falls back to live rendering rather than 500-ing on a document somebody needs to look at."""
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            return None
        if not isinstance(data, dict):
            return None
        data.pop("_version", None)
        try:
            view = cls(
                **{
                    k: v
                    for k, v in data.items()
                    if k not in ("issuer", "totals", "lines") and k in cls.__dataclass_fields__
                }
            )
            view.issuer = IssuerView(**{k: v for k, v in (data.get("issuer") or {}).items()
                                        if k in IssuerView.__dataclass_fields__})
            tot = data.get("totals") or {}
            view.totals = TotalsView(
                subtotal=money(tot.get("subtotal")),
                discount=money(tot.get("discount")),
                discount_label=tot.get("discount_label") or "Discount",
                taxable=money(tot.get("taxable")),
                tax=money(tot.get("tax")),
                tax_label=tot.get("tax_label") or "",
                total=money(tot.get("total")),
            )
            view.lines = [
                LineView(
                    description=ln.get("description") or "",
                    detail=ln.get("detail"),
                    qty=money(ln.get("qty"), default=Decimal("1.00")),
                    unit=ln.get("unit") or DEFAULT_UNIT,
                    unit_price=money(ln.get("unit_price")),
                    source=ln.get("source") or "manual",
                )
                for ln in (data.get("lines") or [])
                if isinstance(ln, dict)
            ]
            view.scope = [str(s) for s in (data.get("scope") or [])]
        except (TypeError, ValueError):
            return None
        return view


def issuer_view(brand) -> IssuerView:
    """Flatten a :class:`cream.models.Brand` (or ``None``) into the renderer's issuer block."""
    if brand is None:
        return IssuerView()
    return IssuerView(
        company_name=brand.company_name or "",
        address=brand.address,
        email=brand.email,
        phone=brand.phone,
        website=brand.website,
        tax_id=brand.tax_id,
        logo_data_uri=brand.logo_data_uri,
        accent_color=brand.accent_color or "#0f766e",
        font_stack=brand.font_stack or IssuerView.font_stack,
        payment_instructions=brand.payment_instructions,
        footer_terms=brand.footer_terms,
    )


def build_view(doc, brand=None, *, totals: TotalsView | None = None) -> DocumentView:
    """Flatten a LIVE ``Document`` (saved or transient) plus branding into a view.

    ``totals`` is injected by the caller (``service.totals``) so this module stays free of the totals
    math and the two can never disagree about which one owns it.
    """
    from cream.service import totals as compute_totals  # local: service imports models, not viewmodel

    scope: list[str] = []
    if doc.scope_json:
        try:
            parsed = json.loads(doc.scope_json)
            if isinstance(parsed, list):
                scope = [str(s) for s in parsed]
        except (TypeError, ValueError):
            scope = []

    return DocumentView(
        kind=doc.kind.value,
        status=doc.status.value,
        number=doc.number,
        title=doc.title or "",
        currency=doc.currency or "USD",
        reference=doc.reference,
        issuer=issuer_view(brand),
        bill_to_name=doc.bill_to_name,
        bill_to_attn=doc.bill_to_attn,
        bill_to_address=doc.bill_to_address,
        bill_to_email=doc.bill_to_email,
        issued_at=_fmt_date(doc.issued_at),
        valid_until=_fmt_date(doc.valid_until),
        due_date=_fmt_date(doc.due_date),
        window_start=_fmt_date(doc.window_start),
        window_end=_fmt_date(doc.window_end),
        lines=[
            LineView(
                description=li.description or "",
                detail=li.detail,
                qty=money(li.qty, default=Decimal("1.00")),
                unit=li.unit or DEFAULT_UNIT,
                unit_price=money(li.unit_price),
                source=li.source or "manual",
            )
            for li in doc.line_items
        ],
        totals=totals if totals is not None else compute_totals(doc),
        notes=doc.notes,
        scope=scope,
        authorization_required=bool(doc.authorization_required),
        signatory_name=doc.signatory_name,
        signatory_title=doc.signatory_title,
        roe_terms=doc.roe_terms,
    )


def view_for(doc, brand=None) -> DocumentView:
    """The view a *reader* of ``doc`` should see.

    An issued document renders from its frozen snapshot; a draft renders live. This is the whole
    "client copy == your copy" guarantee, and it lives in one function so no route can forget it.
    """
    if doc.snapshot_json:
        snap = DocumentView.from_snapshot(doc.snapshot_json)
        if snap is not None:
            snap.status = doc.status.value  # status keeps moving after issue (sent / accepted / void)
            return snap
    return build_view(doc, brand)


def snapshot_dates(view: DocumentView, doc) -> None:
    """Re-stamp a freshly-issued view's dates from the document (issue sets ``issued_at`` after the view
    is built during the same transaction)."""
    view.issued_at = _fmt_date(doc.issued_at)
    view.number = doc.number
