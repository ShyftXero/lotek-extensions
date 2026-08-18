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
_SAFE_NAME_MAX = _NAME_MAX - 32 - 1  # len(uuid4().hex) + len("_")
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
