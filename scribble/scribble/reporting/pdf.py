"""docx → PDF via a Gotenberg service.

Why this route (not the browser's print-to-PDF): Chromium's print engine does not implement the CSS
Paged-Media ``target-counter``, so an HTML table of contents can never show real page numbers. Word /
LibreOffice compute them from a TOC field + a ``PAGE`` footer, updated at load — which Gotenberg's
LibreOffice route does when asked (``updateIndexes=true``), so the TOC entries and footers pick up their
real pages at convert time. The ``default.docx`` template already carries the TOC field, the ``PAGE``/
``NUMPAGES`` footer and ``w:updateFields`` (see ``report_templates/build_default_docx.py``), so the raw
``.docx`` export is an editable document whose TOC updates in the client's Word with no round-trip, and the
PDF path is a pure bytes-over-HTTP conversion — no LibreOffice on the app host, no temp files.

Gotenberg (``gotenberg/gotenberg:8``) runs LibreOffice in its own container with no egress, which is the
isolation that matters: soffice renders scan-derived, attacker-influenced report text, and here it never
touches the app's host, profile, or filesystem. Core auto-provisions the container on enable via
``HostServices.ensure_service_container`` (constrained spec, 127.0.0.1-bound); an operator can also point
``pdf_service_url`` at an external Gotenberg.
"""
from __future__ import annotations

import urllib.error
import urllib.request
import uuid

_DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


class PdfConversionError(RuntimeError):
    """Gotenberg was unreachable or the conversion failed — the caller falls back to offering the docx."""


def _multipart(fields: dict[str, str], docx_bytes: bytes, filename: str) -> tuple[bytes, str]:
    """Encode one ``.docx`` file part (Gotenberg's ``files`` field) plus form fields as
    ``multipart/form-data``. Hand-rolled to avoid a ``requests`` dependency for a single POST — the body
    shape is tiny and fixed."""
    boundary = "----scribble" + uuid.uuid4().hex
    crlf = b"\r\n"
    parts: list[bytes] = []
    for name, value in fields.items():
        parts += [
            f"--{boundary}".encode(),
            f'Content-Disposition: form-data; name="{name}"'.encode(),
            b"",
            value.encode(),
        ]
    parts += [
        f"--{boundary}".encode(),
        f'Content-Disposition: form-data; name="files"; filename="{filename}"'.encode(),
        f"Content-Type: {_DOCX_MIME}".encode(),
        b"",
        docx_bytes,
        f"--{boundary}--".encode(),
        b"",
    ]
    return crlf.join(parts), boundary


def gotenberg_convert(
    docx_bytes: bytes,
    *,
    service_url: str,
    token: str | None = None,
    timeout: int = 180,
    filename: str = "report.docx",
) -> bytes:
    """POST ``.docx`` bytes to Gotenberg's LibreOffice route and return the PDF bytes.

    ``updateIndexes=true`` rebuilds the TOC page numbers; ``exportBookmarks=true`` keeps the heading
    outline as PDF bookmarks. ``token`` (if set) is sent as a Bearer header for an auth-fronted Gotenberg.
    Raises :class:`PdfConversionError` on any transport or non-2xx response so a route can offer the docx
    instead of 500."""
    body, boundary = _multipart(
        {"updateIndexes": "true", "exportBookmarks": "true"}, docx_bytes, filename
    )
    url = service_url.rstrip("/") + "/forms/libreoffice/convert"
    headers = {"Content-Type": f"multipart/form-data; boundary={boundary}"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")  # noqa: S310 — operator-set URL
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            pdf = resp.read()
    except urllib.error.HTTPError as exc:
        detail = (exc.read() or b"")[:400]
        raise PdfConversionError(f"Gotenberg returned {exc.code}: {detail!r}") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise PdfConversionError(f"Gotenberg unreachable at {url}: {exc}") from exc
    if not pdf.startswith(b"%PDF"):
        raise PdfConversionError("Gotenberg response was not a PDF")
    return pdf
