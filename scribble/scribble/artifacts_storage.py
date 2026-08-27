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
from pathlib import Path
from typing import Any

from werkzeug.utils import secure_filename

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


def _bounded_name(safe_name: str) -> str:
    """Truncate a post-``secure_filename`` basename to what the filesystem will actually accept, keeping a
    short trailing extension so the stored file still looks like a ``.png``.

    ``secure_filename`` NFKD-normalizes, and normalization can EXPAND: "½" becomes "1⁄2" -> "12", so a
    204-character unicode filename comes out 404 characters long. Bounding the CALLER's characters at the API
    boundary therefore cannot close this — 200 "½" plus ".png" passes a 222-character cap and still overran
    ``NAME_MAX``, and ``target.write_bytes`` raised ``OSError: [Errno 36] File name too long`` -> a 500 with
    no artifact stored. Measured, reproduced, and fixed HERE rather than at the boundary because this is the
    layer that knows the final name: the same expansion reached this function from the cookie upload route
    too, which no API-side cap covers at all. The API's own 222 cap stays, as the fast, honest 400 for a
    caller that sends an absurd name.
    """
    if len(safe_name) <= _SAFE_NAME_MAX:
        return safe_name
    stem, dot, ext = safe_name.rpartition(".")
    if not dot or not ext or len(ext) > _EXT_MAX:
        return safe_name[:_SAFE_NAME_MAX]
    keep = _SAFE_NAME_MAX - len(ext) - 1
    return f"{stem[:keep]}.{ext}" if keep > 0 else safe_name[:_SAFE_NAME_MAX]


def safe_join(root: Path, rel: str) -> Path:
    """Resolve ``rel`` under ``root``, raising ``ValueError`` if it would escape (``..`` traversal,
    absolute-path override, symlink games, etc.). Mirrors Lotek's ``files_api.safe_join``."""
    root = Path(root).resolve()
    target = (root / rel).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"unsafe storage path: {rel!r}") from exc
    return target


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


def save_bytes(cfg: Any, engagement_id: int, filename: str, data: bytes) -> tuple[str, str, int]:
    """Write ``data`` under ``cfg.artifact_root/<engagement_id>/`` with a collision-proof name.

    Returns ``(storage_path, sha256, byte_size)`` where ``storage_path`` is relative to
    ``cfg.artifact_root`` — that relative path is what the ``Artifact`` row stores.
    """
    root = Path(cfg.artifact_root)
    root.mkdir(parents=True, exist_ok=True)

    # Bounded AFTER secure_filename, because that is the step that can make the name LONGER (see
    # ``_bounded_name``) — an unbounded value here is ENAMETOOLONG, i.e. a 500 on a legitimate upload.
    safe_name = _bounded_name(secure_filename(filename) or "artifact")
    unique_name = f"{uuid.uuid4().hex}_{safe_name}"
    storage_path = f"{int(engagement_id)}/{unique_name}"

    target = safe_join(root, storage_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)

    sha256 = hashlib.sha256(data).hexdigest()
    return storage_path, sha256, len(data)


def resolve_path(cfg: Any, storage_path: str) -> Path:
    """Resolve a stored ``storage_path`` back to an on-disk ``Path``, confined to ``artifact_root``."""
    return safe_join(Path(cfg.artifact_root), storage_path)


def delete_file(cfg: Any, storage_path: str) -> None:
    """Best-effort delete of the on-disk file for ``storage_path``.

    Swallows an unsafe/already-missing path so callers can always clean up the DB row even if the file
    is gone or the stored path is somehow bogus.
    """
    try:
        target = resolve_path(cfg, storage_path)
    except ValueError:
        return
    if target.is_file():
        target.unlink()


def store_bytes(
    engagement: Any,
    filename: str,
    data: bytes,
    *,
    content_type: str | None = None,
) -> tuple[str | None, str, int]:
    """Put ``data`` in the CORE object store, returning ``(object_id, sha256, byte_size)``.

    ``object_id`` is None when the store could not be used, and the caller must then fall back to
    :func:`save_bytes` (local disk). Three ways that happens, all legitimate deployments rather than
    errors:

    * the extension is unmounted, or this deployment runs no object store (``host.objects()`` None);
    * the engagement has no ``core_engagement_id`` — scribble engagements may stand alone, and the
      host authorizes a put against a CORE engagement, so there is nothing to authorize against;
    * the host refuses the put.

    A ``PermissionError`` is NOT swallowed. The host raises it when the actor lacks an operator
    capability on the engagement, and quietly writing the bytes to disk instead would turn a refused
    upload into a successful one — the deny would be enforced by the store and defeated by the
    fallback. Everything else degrades to disk.

    THE ID TRAP: the host resolves a CORE ``Engagement``. Scribble's own PK is a different id space,
    and passing it here would fail the authorization lookup rather than erroring loudly — this repo
    has already taken production down once by crossing those two spaces.
    """
    from . import host as _host

    surface = _host.objects()
    if surface is None:
        return None, hashlib.sha256(data).hexdigest(), len(data)

    core_id = getattr(engagement, "core_engagement_id", None)
    if not core_id:
        return None, hashlib.sha256(data).hexdigest(), len(data)

    guessed = content_type or mimetypes.guess_type(filename)[0] or "application/octet-stream"
    ref = surface.put(
        _host.actor(),
        # A REAL core ObjectKind member. "scribble_evidence" is not one (artifact/report/screenshot/
        # evidence), and core validated the kind only AFTER uploading the bytes — so the first
        # evidence upload would have left an orphan blob in the bucket with no row, which nothing
        # reclaims. Core now validates first; this passes a member either way.
        kind="evidence",
        stream=io.BytesIO(data),
        content_type=guessed,
        filename=filename,
        engagement_id=core_id,
    )
    return str(getattr(ref, "id", "") or "") or None, hashlib.sha256(data).hexdigest(), len(data)


#: Marks a reference that points at the CORE object store rather than at ``artifact_root``.
#: A prefix keeps every existing ``storage_path -> bytes`` reader signature intact — there are five
#: call sites across the report, docx and download paths, and widening all of them to take an
#: ``Artifact`` would have been a much larger diff for no extra safety.
OBJECT_REF_PREFIX = "obj:"


def artifact_ref(artifact: Any) -> str:
    """Where THIS artifact's bytes live: an ``obj:<uuid>`` store reference, or its disk path.

    One helper so the map-builders and the readers cannot drift — a builder that emitted a raw path
    for a store-backed row would silently render an empty gallery, which is exactly the kind of
    quiet evidence loss this cutover exists to prevent.
    """
    object_id = getattr(artifact, "object_id", None)
    return f"{OBJECT_REF_PREFIX}{object_id}" if object_id else (artifact.storage_path or "")


def read_object_bytes(ref: str, max_bytes: int) -> bytes | None:
    """Bytes for an ``obj:`` reference, or None (absent, too large, unreadable, or not visible).

    Streams and stops at ``max_bytes`` rather than reading first and checking after: the on-disk path
    refuses an oversized artifact without loading it, and the store path must not be the one that
    pulls a gigabyte into the renderer.
    """
    from . import host as _host

    surface = _host.objects()
    if surface is None or not ref.startswith(OBJECT_REF_PREFIX):
        return None
    try:
        object_id = uuid.UUID(ref[len(OBJECT_REF_PREFIX):])
    except (TypeError, ValueError):
        return None
    try:
        with surface.open(_host.actor(), object_id) as body:
            data = body.read(max_bytes + 1)
    except (KeyError, PermissionError, RuntimeError, OSError):
        return None
    return None if data is None or len(data) > max_bytes else data
