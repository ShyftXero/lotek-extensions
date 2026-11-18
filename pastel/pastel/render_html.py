"""Markdown paste body -> sanitized HTML for the rendered view.

A paste body is FULLY attacker-authored (anyone with write, or the machine API), so rendering is a
security boundary: Markdown produces HTML, then nh3 (ammonia, a Rust sanitizer) strips everything
outside a fixed allowlist — no ``<script>``, no event handlers, no ``javascript:`` / ``data:`` URLs.
``nh3`` adds ``rel="noopener noreferrer"`` to links by default. The ``/raw`` view never touches this
module; it serves the stored source as ``text/plain``.
"""

from __future__ import annotations

import markdown as _md
import nh3
from markupsafe import Markup

# nh3's default allowlist is already safe; extend it so Markdown's tables and figures survive the clean.
_TAGS: set[str] = set(nh3.ALLOWED_TAGS) | {
    "table", "thead", "tbody", "tfoot", "tr", "th", "td", "caption", "colgroup", "col", "hr",
}


def render_markdown(body: str) -> Markup:
    """Render stored markdown to safe HTML. Safe to mark as ``Markup`` because nh3 has removed anything
    that is not in the allowlist."""
    raw = _md.markdown(body or "", extensions=["fenced_code", "tables", "sane_lists", "nl2br"])
    return Markup(nh3.clean(raw, tags=_TAGS))
