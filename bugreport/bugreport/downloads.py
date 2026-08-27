"""Serving a file we did not write — the ONE place download responses are built.

Both the authenticated route and the anonymous share route come through here, deliberately. Two
builders would be two chances to forget a header, and the headers ARE the security control: this
extension accepts arbitrary bytes, so the rule is that nothing user-supplied ever gets to run in
lotek's origin.

What that means concretely:

* **`Content-Disposition: attachment` unless the type is a verified raster image.** `INLINE_SAFE_TYPES`
  is a four-entry allowlist of formats browsers render but never execute, and a file only carries one
  of those types if its MAGIC BYTES agreed with the uploader's claim at upload time
  (``service._sniff``). Everything else — including anything HTML-shaped, and `image/svg+xml`, which is
  a script-capable document — is `application/octet-stream` and downloads.
* **`X-Content-Type-Options: nosniff`**, so a browser cannot decide for itself that our
  `application/octet-stream` is really HTML.
* **`Content-Security-Policy: default-src 'none'; sandbox`**, which neuters the response even if the
  two rules above were somehow both wrong. Defence in depth at the point it is relied on.
* **`Referrer-Policy: no-referrer`**, because on the share route the URL *is* the credential. Without
  it, any link the file leads to would receive the capability token in the `Referer` header.
"""

from __future__ import annotations

from flask import Response

from bugreport.models import INLINE_SAFE_TYPES, Attachment


def _disposition(row: Attachment) -> str:
    kind = "inline" if row.content_type in INLINE_SAFE_TYPES else "attachment"
    # RFC 6266/5987: a plain `filename=` for ASCII plus `filename*=` for everything else, so a
    # non-ASCII name survives without smuggling quotes or newlines into the header. `filename` was
    # already stripped of quotes and control characters at upload (`service._clean_filename`).
    ascii_name = row.filename.encode("ascii", "replace").decode("ascii")
    quoted = ascii_name.replace('"', "")
    utf8 = row.filename.encode("utf-8").hex()
    utf8 = "".join(f"%{utf8[i:i + 2]}" for i in range(0, len(utf8), 2))
    return f"{kind}; filename=\"{quoted}\"; filename*=UTF-8''{utf8}"


def send_attachment(row: Attachment, blobs, *, chunk_size: int = 65536) -> Response:
    """Stream one attachment back with the full header set. Raises ``KeyError`` if the bytes are gone."""

    def _stream():
        with blobs.open(row.id) as body:
            while True:
                chunk = body.read(chunk_size)
                if not chunk:
                    break
                yield chunk

    resp = Response(_stream(), mimetype=row.content_type)
    resp.headers["Content-Disposition"] = _disposition(row)
    resp.headers["Content-Length"] = str(row.size)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Content-Security-Policy"] = "default-src 'none'; sandbox"
    resp.headers["Referrer-Policy"] = "no-referrer"
    # A capability URL must not be cached by a shared proxy on the way to one recipient.
    resp.headers["Cache-Control"] = "private, max-age=0, no-store"
    return resp
