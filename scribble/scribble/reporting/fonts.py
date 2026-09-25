"""Report font selection.

An operator picks a body face and a code face; the render applies them and the PDF bakes them in — no
raw-docx editing. The template ships with its defaults baked into ``default.docx``; :func:`remap_fonts`
swaps those default families for the chosen ones across the whole document at render time.

The choosable set is exactly the faces the ``lotek-gotenberg`` image has (extensions/scribble/gotenberg):
the baked Inter + JetBrains Mono, plus the base image's Liberation / DejaVu. A font that is NOT in the
image would be substituted by LibreOffice at convert time, so the menu is deliberately constrained to what
will actually render — an operator's own upload is a separate (font-embedding) path, not this list.
"""
from __future__ import annotations

from docx.oxml.ns import qn

# The template's baked-in defaults (report_templates/build_default_docx.py imports these, so the remap's
# "family to replace" is always exactly what the template wrote).
DEFAULT_BODY_FONT = "Inter"
DEFAULT_CODE_FONT = "JetBrains Mono"

# (value, label). ``value`` is the exact fontconfig family name present in the lotek-gotenberg image.
BODY_FONT_CHOICES: list[tuple[str, str]] = [
    ("Inter", "Inter (default)"),
    ("Liberation Sans", "Liberation Sans (Arial-metric)"),
    ("DejaVu Sans", "DejaVu Sans"),
]
CODE_FONT_CHOICES: list[tuple[str, str]] = [
    ("JetBrains Mono", "JetBrains Mono (default)"),
    ("DejaVu Sans Mono", "DejaVu Sans Mono"),
    ("Liberation Mono", "Liberation Mono (Courier-metric)"),
]

_BODY_NAMES = {v for v, _ in BODY_FONT_CHOICES}
_CODE_NAMES = {v for v, _ in CODE_FONT_CHOICES}
_FONT_ATTRS = ("w:ascii", "w:hAnsi", "w:cs", "w:eastAsia")


def valid_body_font(name: str | None) -> str | None:
    """The name if it is a known body choice, else None — so an unknown/removed setting falls back to the
    template default rather than silently rendering a font the image lacks."""
    name = (name or "").strip()
    return name if name in _BODY_NAMES else None


def valid_code_font(name: str | None) -> str | None:
    name = (name or "").strip()
    return name if name in _CODE_NAMES else None


def remap_fonts(doc, body_font: str | None = None, code_font: str | None = None) -> None:
    """Swap the template's baked default faces for the operator's chosen ones, in every ``w:rFonts`` across
    the document body AND the styles part (so styles, docDefaults, and explicit run fonts all move). A None,
    unknown, or identical choice is a no-op for that axis; if nothing maps, the document is untouched."""
    mapping: dict[str, str] = {}
    if body_font and body_font != DEFAULT_BODY_FONT and body_font in _BODY_NAMES:
        mapping[DEFAULT_BODY_FONT] = body_font
    if code_font and code_font != DEFAULT_CODE_FONT and code_font in _CODE_NAMES:
        mapping[DEFAULT_CODE_FONT] = code_font
    if not mapping:
        return
    roots = [doc.element]
    styles = getattr(doc, "styles", None)
    if styles is not None:
        roots.append(styles.element)
    tag = qn("w:rFonts")
    for root in roots:
        for rf in root.iter(tag):
            for attr in _FONT_ATTRS:
                current = rf.get(qn(attr))
                if current in mapping:
                    rf.set(qn(attr), mapping[current])
