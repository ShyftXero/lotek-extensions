"""One exporter seam for every report format.

Every exporter is a pure function of the **same two inputs the user cares about** — the vuln data
(a frozen :class:`ReportContext` from :func:`build_report_context`) and the presentation info it needs
(:class:`ExportOptions`: theme name, layout, template, evidence-byte reader, override lookup) — and
returns rendered ``bytes``. Formats live in one :data:`EXPORTERS` registry so a route dispatches with a
lookup instead of a hand-rolled ``if fmt == …`` ladder, and adding a format (or swapping the PDF engine)
is a one-line registration rather than an edit to every dispatch site.

Two coupling facts drive the shape:

* **Return type is normalised to bytes.** ``render_report_html``/``_csv``/``_json`` return ``str``; the
  adapters encode UTF-8 so every ``Exporter.render`` yields ``bytes`` and the route never branches on it.
* **The inline-image placeholder is renderer-specific** (``render_html`` bakes ``/__scribble_inline__/``,
  ``render_docx`` bakes ``/__scribble_docx_inline__/``), so each exporter carries its own
  :attr:`Exporter.inline_url` and the caller builds the ``ReportContext`` with *that* factory. csv/json
  carry the #626 hashes rather than bytes, so they need none.

PDF is the swap point: it composes the docx exporter and hands the bytes to a **pluggable conversion
backend** (:data:`_PDF_BACKENDS`). The default is ``gotenberg`` — a containerized LibreOffice reached over
HTTP (``pdf.gotenberg_convert``), configured via :func:`configure_gotenberg`. A different engine
(a local soffice, a hosted converter) is another :func:`register_pdf_backend` + :func:`set_pdf_backend`,
with no exporter or route change (see ``plans/feat-report-pdf-deliverable.md``).
"""
from __future__ import annotations

import os
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from scribble.reporting.context import ReportContext

# The image + port the auto-provisioned PDF converter runs. Gotenberg's LibreOffice route listens on 3000
# inside the container; core binds it to 127.0.0.1 and hands back the URL.
GOTENBERG_IMAGE = "gotenberg/gotenberg:8"
GOTENBERG_PORT = 3000

ArtifactBytes = Callable[[str], "bytes | None"]
OverrideLookup = Callable[[str], "str | None"]

# The one home for the docx media type (was defined independently in report_docx_api.py and api_pat.py).
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


@dataclass(frozen=True)
class ExportOptions:
    """Everything an exporter may need beyond the ``ReportContext``. Each exporter reads only the fields
    it needs: csv/json use none, docx/pdf use ``artifact_bytes``, html/zip use the whole theme stack."""

    artifact_bytes: ArtifactBytes | None = None
    engagement_url: str | None = None
    dashboard_url: str | None = None
    layout: str | None = None
    theme: str | None = None
    template: str | None = None
    override_lookup: OverrideLookup | None = None
    override_theme_names: tuple[str, ...] = ()
    # Optional provider of the engagement's RAW job loot for the zip bundle: called (once, lazily) by the
    # zip exporter and yields ``(arcname, bytes)`` streamed from the object store. Only the route sets it
    # (it holds the host objects seam + actor); other formats ignore it.
    loot: Callable[[], Iterable[tuple[str, bytes]]] | None = None


@dataclass(frozen=True)
class Exporter:
    """One output format: its identity, HTTP framing, and a ``(ctx, opts) -> bytes`` renderer.

    ``inline_url`` is the renderer-specific inline-image placeholder factory the caller must feed to
    ``build_report_context(engagement, artifact_url=…)`` so this exporter's images resolve; ``None`` for
    formats that embed no evidence bytes (csv/json)."""

    name: str
    media_type: str
    extension: str
    render: Callable[[ReportContext, ExportOptions], bytes]
    inline_url: Callable[[str | None], str] | None = None


EXPORTERS: dict[str, Exporter] = {}


def register_exporter(exporter: Exporter) -> Exporter:
    EXPORTERS[exporter.name] = exporter
    return exporter


def get_exporter(name: str | None) -> Exporter | None:
    """Look up an exporter by (case-folded) format name; ``None`` if unknown."""
    return EXPORTERS.get((name or "").strip().lower())


# --------------------------------------------------------------------------- PDF conversion backends

class PdfExportError(RuntimeError):
    """A configured PDF backend was missing or failed — the route turns this into a clean 503 rather
    than a 500, so an absent LibreOffice/Gotenberg reads as "PDF unavailable", not a crash."""


PdfConvert = Callable[[bytes], bytes]
_PDF_BACKENDS: dict[str, PdfConvert] = {}
_pdf_backend = "gotenberg"   # docx->PDF engine; scribble configures the Gotenberg URL on enable

# The Gotenberg service the backend targets. scribble sets this on enable — from `pdf_service_url` if an
# operator configured an external Gotenberg, else the 127.0.0.1 URL that `ensure_service_container`
# returns for the auto-provisioned container — and again whenever the setting changes (config<->GUI).
_gotenberg_url: str | None = None
_gotenberg_token: str | None = None


def configure_gotenberg(url: str | None, token: str | None = None) -> None:
    global _gotenberg_url, _gotenberg_token
    _gotenberg_url = url or None
    _gotenberg_token = token or None


def gotenberg_url() -> str | None:
    return _gotenberg_url


def ensure_pdf_service() -> None:
    """Point the PDF backend at a Gotenberg service, idempotently. No-op once configured.

    This is what makes "install + enable = PDF works" true with zero setup: the FIRST time a PDF is
    rendered, this asks the host to auto-provision the managed Gotenberg container (idempotent — a no-op
    once it's running) and configures the backend with the 127.0.0.1 URL it hands back. It runs here,
    lazily, rather than in ``register()`` because the host seam is injected AFTER an extension registers
    (the injection-ordering trap).

    An operator running their own Gotenberg sets ``SCRIBBLE_PDF_SERVICE_URL`` (+ optional
    ``SCRIBBLE_PDF_SERVICE_TOKEN``) and that wins — no container is provisioned. A standalone scribble with
    no host seam simply stays unconfigured, so a PDF request degrades to ``PdfExportError`` -> 503 rather
    than crashing."""
    if _gotenberg_url:
        return
    token = os.environ.get("SCRIBBLE_PDF_SERVICE_TOKEN", "").strip() or None
    external = os.environ.get("SCRIBBLE_PDF_SERVICE_URL", "").strip()
    if external:
        configure_gotenberg(external, token)
        return
    from scribble.host import host_hook

    ensure = host_hook("ensure_service_container")
    if ensure is None:
        return  # standalone / no host — leave unconfigured (PDF then 503s, never crashes)
    result = ensure("gotenberg", GOTENBERG_IMAGE, GOTENBERG_PORT)
    url = result.get("url") if isinstance(result, dict) else None
    if url:
        configure_gotenberg(url, token)


def register_pdf_backend(name: str, fn: PdfConvert) -> None:
    _PDF_BACKENDS[name] = fn


def set_pdf_backend(name: str) -> None:
    global _pdf_backend
    _pdf_backend = name


def pdf_backend_name() -> str:
    return _pdf_backend


def convert_docx_to_pdf(docx_bytes: bytes) -> bytes:
    """Convert rendered ``.docx`` bytes to PDF via the currently-selected backend. Any backend failure
    (soffice absent, Gotenberg unreachable) surfaces as :class:`PdfExportError`."""
    fn = _PDF_BACKENDS.get(_pdf_backend)
    if fn is None:
        raise PdfExportError(f"no PDF backend registered under {_pdf_backend!r}")
    try:
        return fn(docx_bytes)
    except PdfExportError:
        raise
    except Exception as exc:  # noqa: BLE001 — normalise every backend fault to one route-visible error
        raise PdfExportError(f"PDF backend {_pdf_backend!r} failed: {exc}") from exc


def _gotenberg_backend(docx_bytes: bytes) -> bytes:
    from scribble.reporting.pdf import gotenberg_convert

    ensure_pdf_service()  # provision/configure on first use (idempotent)
    if not _gotenberg_url:
        raise PdfExportError("no Gotenberg service available (auto-provision failed and "
                             "SCRIBBLE_PDF_SERVICE_URL is unset)")
    return gotenberg_convert(docx_bytes, service_url=_gotenberg_url, token=_gotenberg_token)


register_pdf_backend("gotenberg", _gotenberg_backend)


# --------------------------------------------------------------------------- renderer adapters

def _b(value: str | bytes) -> bytes:
    return value.encode("utf-8") if isinstance(value, str) else value


def _html_inline_url(storage_path: str | None) -> str:
    from scribble.reporting.render_html import make_inline_artifact_url

    return make_inline_artifact_url(storage_path)


def _docx_inline_url(storage_path: str | None) -> str:
    from scribble.reporting.render_docx import make_inline_artifact_url

    return make_inline_artifact_url(storage_path)


def _render_html(ctx: ReportContext, opts: ExportOptions) -> bytes:
    from scribble.reporting.render_html import render_report_html

    return _b(render_report_html(
        ctx,
        inline_assets=True,
        artifact_bytes=opts.artifact_bytes,
        engagement_url=opts.engagement_url,
        dashboard_url=opts.dashboard_url,
        layout=opts.layout,
        theme=opts.theme,
        template=opts.template,
        override_lookup=opts.override_lookup,
        override_theme_names=opts.override_theme_names,
    ))


def _render_docx(ctx: ReportContext, opts: ExportOptions) -> bytes:
    from scribble.reporting.render_docx import render_report_docx

    return render_report_docx(ctx, artifact_bytes=opts.artifact_bytes)


def _render_csv(ctx: ReportContext, opts: ExportOptions) -> bytes:
    from scribble.reporting.render_csv import render_report_csv

    return _b(render_report_csv(ctx))


def _render_json(ctx: ReportContext, opts: ExportOptions) -> bytes:
    from scribble.reporting.render_json import render_report_json

    return _b(render_report_json(ctx))


def _render_zip(ctx: ReportContext, opts: ExportOptions) -> bytes:
    from scribble.reporting.render_html import export_zip

    return export_zip(
        ctx,
        opts.artifact_bytes,
        layout=opts.layout,
        theme=opts.theme,
        template=opts.template,
        override_lookup=opts.override_lookup,
        loot=opts.loot() if opts.loot else None,
    )


def _render_pdf(ctx: ReportContext, opts: ExportOptions) -> bytes:
    return convert_docx_to_pdf(_render_docx(ctx, opts))


register_exporter(Exporter("html", "text/html; charset=utf-8", "html", _render_html, _html_inline_url))
register_exporter(Exporter("docx", DOCX_MIME, "docx", _render_docx, _docx_inline_url))
register_exporter(Exporter("csv", "text/csv", "csv", _render_csv))
register_exporter(Exporter("json", "application/json", "json", _render_json))
register_exporter(Exporter("zip", "application/zip", "zip", _render_zip, _html_inline_url))
register_exporter(Exporter("pdf", "application/pdf", "pdf", _render_pdf, _docx_inline_url))
