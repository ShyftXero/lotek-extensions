"""Filesystem storage helpers for artifacts (WS5, PLAN.md §7, plans/CONTRACTS.md #8).

Bytes live on disk under ``ScribbleConfig.artifact_root`` (``instance/artifacts/``); the ``Artifact``
row only ever stores a *relative* ``storage_path`` plus ``sha256``/``content_type``/``byte_size``
metadata — never base64-in-DB.

``safe_join`` mirrors Lotek's ``files_api.safe_join`` confinement: every path is resolved and checked to
still live under the root before any read/write/delete, closing off ``..`` traversal regardless of what
a caller passes as ``storage_path``.
"""

from __future__ import annotations

import hashlib
import io
import mimetypes
import uuid
from typing import Any

# A short allowlist of magic-byte signatures used to sniff content-type from bytes rather than trust a
# (possibly attacker-supplied) filename extension. Falls back to extension-based guessing, then a
# generic binary type — mirrors Lotek's untrusted-file handling (files_api.py / routes.py
# job_file_download): never assume the extension is honest; the API layer always forces
# ``Content-Disposition: attachment`` when serving artifacts back out.
_MAGIC_SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"%PDF-", "application/pdf"),
    (b"PK\x03\x04", "application/zip"),  # also docx/xlsx/pptx at this granularity; good enough to sniff
)


# The FILESYSTEM's limit on one path component, and the budget left for the caller's part of the name once
# ``save_bytes`` has prefixed "<uuid4hex>_". NAME_MAX is 255 bytes on Linux/ext4 (and 255 on APFS/HFS+);
# ``secure_filename`` returns pure ASCII (it NFKD-normalizes then ``encode("ascii", "ignore")``), so
# characters and bytes are the same count by the time we measure.
_NAME_MAX = 255
# Public (no leading underscore): the budget left for the CALLER's filename once ``save_bytes`` has
# prefixed "<uuid4hex>_" (33 chars). Exported so every upload route that wants to reject an over-long
# name up front (a 400, before writing any bytes) uses the same number this module truncates to,
# rather than each route computing its own copy of "255 - 32 - 1" and the two silently drifting apart.
SAFE_NAME_MAX = _NAME_MAX - 32 - 1
_SAFE_NAME_MAX = SAFE_NAME_MAX  # internal alias kept for the docstring/comments below
# Longest trailing ".<ext>" worth preserving through a truncation; anything longer is not an extension, it
# is the tail of a very long name, and keeping it would eat the whole budget.
_EXT_MAX = 16


def _sniff_magic(data: bytes) -> str | None:
    for sig, ctype in _MAGIC_SIGNATURES:
        if data.startswith(sig):
            return ctype
    return None


def guess_content_type(filename: str, data: bytes | None = None) -> str:
    """Sniff content-type from magic bytes first (extensions lie), fall back to the filename's
    extension, then a generic binary type. Never trust the extension alone for untrusted uploads."""
    if data:
        sniffed = _sniff_magic(data)
        if sniffed:
            return sniffed
    guessed, _ = mimetypes.guess_type(filename)
    return guessed or "application/octet-stream"


def delete_file(storage_path: str) -> None:
    """Best-effort delete of the bytes behind ``storage_path`` — a local file, OR an ``obj:`` reference
    into the core object store.

    The store branch lives HERE, not at the call sites, because every delete in scribble already
    funnels through this one function: artifact delete, finding delete and engagement delete, each on
    both the cookie and the machine surface — six call sites, five of which only ever saw a raw
    ``storage_path``. A store-backed row deleted through any of them would have dropped the DB row and
    left the blob in the bucket with nothing left pointing at it, which no code path would ever notice
    again. That is the same orphan-blob class the core review caught on the write side.

    The store delete is a TOMBSTONE (``HostObjects.delete``); core's leader-only GC reclaims the bytes.

    Swallows every failure — an unsafe path, a missing file, an unmounted host, a refused delete — so
    the caller can always clean up the DB row. A refused store delete therefore leaks the blob rather
    than 500-ing after the rows are already gone; core's GC is the backstop for that, not this.
    """
    from . import host as _host

    object_id = object_id_of(storage_path)
    surface = _host.objects()
    if object_id is None or surface is None:
        return  # a pre-cutover row, or no host: nothing this can reclaim
    try:
        surface.delete(_acting_principal(), object_id)
    except (PermissionError, RuntimeError, OSError, ValueError):
        pass


def _acting_principal():
    """The host principal behind THIS request, whichever transport it arrived on.

    Both hooks are produced by the HOST — the extension chooses which one is present, never what is
    in it, so attribution stays observed rather than supplied. ``pat_actor()`` answers on a machine
    route; ``current_actor()`` answers on a browser route, where ``pat_actor()`` is None because
    ``host_contract.pat_actor()`` reads ``g.api_user_id`` and only PAT authentication sets it.

    Without the fallback the object store was reachable from machine routes ONLY, and that single gap
    is what forced the browser surface onto its own parallel filesystem.
    """
    from . import host as _host
    from .deps import current_actor

    return _host.actor() or current_actor()


def persist_bytes(
    *,
    core_engagement_id: Any,
    filename: str,
    data: bytes,
    content_type: str | None = None,
) -> tuple[str, str, int]:
    """THE way scribble persists an evidence file. Returns ``(reference, sha256, byte_size)``.

    Always the object store. There is NO local-disk arm, and adding one back is the change to refuse:
    the split it created — some evidence in the bucket, some on whichever host served the upload —
    produced bugs that nothing went red for (an evidence gallery that rendered empty), and it made
    "where is this file" a question with two answers.

    Two things are required and both raise rather than degrade:

    * a host object surface. Standalone Scribble is a testbed, not a deployment; its shell supplies a
      mock host (``scribble.testing.wire_mock_host``) so the code path here stays singular.
    * a ``core_engagement_id``. Core files every blob under a core engagement — INV-OBJSTORE-01 makes
      that a database fact via composite FKs — so an unanchored engagement has nowhere in the bucket
      to go. Scribble now obtains one at engagement-create time via ``host.create_engagement``.

    A ``PermissionError`` from the host propagates untouched. Falling back to disk on a refusal would
    mean the store enforced it and the fallback defeated it.
    """
    from . import host as _host

    surface = _host.objects()
    if surface is None:
        raise RuntimeError(
            "no object store: scribble persists evidence only to the host's object store. A "
            "standalone or demo shell must wire one — see scribble.testing.wire_mock_host()"
        )
    if not core_engagement_id:
        raise RuntimeError(
            "this engagement has no core_engagement_id, so its evidence has no tenancy anchor in the "
            "object store — engagements created since the object-store cutover get one automatically"
        )

    guessed = content_type or mimetypes.guess_type(filename)[0] or "application/octet-stream"
    ref = surface.put(
        _acting_principal(),
        # A REAL core ObjectKind member. "scribble_evidence" is not one (artifact/report/screenshot/
        # evidence), and core validated the kind only AFTER uploading the bytes — so the first
        # evidence upload would have left an orphan blob in the bucket with no row, which nothing
        # reclaims. Core now validates first; this passes a member either way.
        kind="evidence",
        stream=io.BytesIO(data),
        content_type=guessed,
        filename=filename,
        engagement_id=core_engagement_id,
    )
    object_id = getattr(ref, "id", None)
    if object_id is None:  # pragma: no cover - a host handing back a ref with no id is a broken host
        raise RuntimeError("the object store returned a reference with no id")
    return f"{OBJECT_REF_PREFIX}{object_id}", hashlib.sha256(data).hexdigest(), len(data)


#: Ceiling for bytes pulled OUT of the object store in one read. The disk readers each carry their
#: own (``report_docx_api`` refuses over 25 MB; ``report_html_api`` historically had none), so the
#: store path states one rather than inheriting an inconsistency: a renderer must never be the thing
#: that pulls a gigabyte of evidence into memory. It sits above what an upload can accept (the host's
#: MAX_CONTENT_LENGTH), so nothing this code stored can be refused by it.
MAX_OBJECT_BYTES = 25 * 1024 * 1024

#: Marks a reference that points at the CORE object store rather than at ``artifact_root``.
OBJECT_REF_PREFIX = "obj:"


def object_id_of(ref: str) -> uuid.UUID | None:
    """The UUID inside an ``obj:<uuid>`` reference, or None when ``ref`` is a plain disk path.

    One parser, because "is this row store-backed?" is asked on the read, delete and download paths,
    and three prefix checks would be three chances to disagree.
    """
    if not ref or not ref.startswith(OBJECT_REF_PREFIX):
        return None
    try:
        return uuid.UUID(ref[len(OBJECT_REF_PREFIX):])
    except (TypeError, ValueError):
        return None


def artifact_bytes(storage_path: str) -> bytes | None:
    """THE ``storage_path -> bytes`` reader every renderer and download path uses.

    Three modules used to define this closure, near-identically, each taking an ``artifact_root`` and
    carrying its own path-escape guard and its own size ceiling (``report_html_api`` had none). That
    duplication is what let the object-store cutover half-land: the builders were switched to emit
    ``obj:`` references and only ONE of the three readers was taught to resolve them, so two renderers
    asked the filesystem for a path spelled ``obj:<uuid>`` and dropped every image without a word.

    With evidence in the object store there is no root to confine, no traversal to guard and no file
    to stat — so the three collapse into this, and there is no longer a set of readers that can
    disagree. A reference this cannot resolve (a pre-cutover disk path) reads as ABSENT, which renders
    as a missing-evidence chip rather than a crash.
    """
    return read_object_bytes(storage_path, MAX_OBJECT_BYTES)


def read_object_bytes(ref: str, max_bytes: int) -> bytes | None:
    """Bytes for an ``obj:`` reference, or None (absent, too large, unreadable, or not visible).

    Streams and stops at ``max_bytes`` rather than reading first and checking after: the on-disk path
    refuses an oversized artifact without loading it, and the store path must not be the one that
    pulls a gigabyte into the renderer.

    One answer for absent, tombstoned, oversized and not-visible alike — deliberately, and matching
    ``HostObjects.open``, which raises ``KeyError`` for "not yours" precisely so a caller cannot
    become an existence oracle for another engagement's evidence.
    """
    from . import host as _host

    object_id = object_id_of(ref)
    surface = _host.objects()
    if surface is None or object_id is None:
        return None
    try:
        with surface.open(_acting_principal(), object_id) as body:
            data = body.read(max_bytes + 1)
    except (KeyError, PermissionError, RuntimeError, OSError):
        return None
    return None if data is None or len(data) > max_bytes else data
