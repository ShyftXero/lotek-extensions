"""Filesystem storage helpers for artifacts (WS5, PLAN.md §7, plans/CONTRACTS.md #8).

Bytes live on disk under ``FractionConfig.artifact_root`` (``instance/artifacts/``); the ``Artifact``
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

    safe_name = secure_filename(filename) or "artifact"
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
