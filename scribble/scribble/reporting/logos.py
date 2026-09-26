"""Cover logo resolution — the ONE place that turns a report board's ``cover_logo_id`` into image bytes.

Both renderers (and the picker thumbnails) go through here, so "which logo does this report show?" has a
single answer: the operator's picked library logo (``ScribbleReportLogo``), or — when none is picked, or the
picked one was deleted — the stock lotek mark bundled beside the docx template. The default lives in the
package (not the object store) so it is always present, standalone or mounted.
"""
from __future__ import annotations

from pathlib import Path

DEFAULT_LOGO_PATH = Path(__file__).resolve().parent.parent / "report_templates" / "lotek_mark.png"
DEFAULT_LOGO_CONTENT_TYPE = "image/png"


def default_logo_bytes() -> bytes:
    """The stock lotek mark bytes (the cover default when a report picks no library logo)."""
    return DEFAULT_LOGO_PATH.read_bytes()


def resolve_cover_logo(board, session) -> tuple[bytes, str]:
    """``(bytes, content_type)`` for a report board's cover logo: the picked library logo, else the stock
    lotek mark. Falls back to the default if the picked logo was deleted (``cover_logo_id`` dangles) — the
    same forgiving posture as an unknown ``?layout=``/``?theme=``."""
    logo_id = getattr(board, "cover_logo_id", None)
    if logo_id and session is not None:
        from scribble.models import ScribbleReportLogo

        row = session.get(ScribbleReportLogo, logo_id)
        if row is not None and row.data:
            return bytes(row.data), (row.content_type or DEFAULT_LOGO_CONTENT_TYPE)
    return default_logo_bytes(), DEFAULT_LOGO_CONTENT_TYPE
