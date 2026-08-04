"""Sanitized block HTML -> docx content (WS8).

Converts the HTML that ``scribble.content.render_html.render_block`` produces (the tag subset it
emits: ``p, h1-h6, ul/ol/li, strong/em/code/s/u, a, blockquote, pre, img, figure/figcaption, br``,
plus transparent ``span``/``div`` wrappers) into a :class:`docxtpl.RichText` object suitable for a
``{{r ... }}`` field in a docxtpl template.

Design note — why RichText and not a ``Subdoc``
-------------------------------------------------
``docxtpl.Subdoc`` (the other officially-suggested approach) requires the ``docxcompose`` package,
which is **not** a declared Scribble dependency (only ``docxtpl`` + ``python-docx`` are). Rather than
add a new dependency for this, this module builds self-contained OOXML fragments directly and hands
them to :class:`docxtpl.RichText` via its ``.xml`` attribute — a documented, supported technique:
``{{r name}}`` fields have their surrounding ``<w:r>/<w:t>`` stripped by docxtpl *before* Jinja runs
(see ``docxtpl.template.DocxTemplate.patch_xml``), so the substituted value must be (and is here) a
fully self-contained sequence of ``<w:r>``/``<w:p>`` elements — which is exactly what lets a single
RichText value span multiple paragraphs, headings, list items and inline images.

Images are embedded eagerly (while building the RichText, before ``tpl.render()`` is called), so the
caller MUST set ``tpl.current_rendering_part = tpl.docx.part`` (and ``tpl.init_docx()`` if the
template hasn't been touched yet) before calling :func:`html_to_richtext`. ``reporting/render_docx.py``
does this once per render.

Defensive by design: malformed/unbalanced HTML, unknown tags, and unresolvable images never raise —
they degrade to escaped plain text / a bracketed placeholder, per WS8's brief ("never crash").
"""

from __future__ import annotations

import io
from collections.abc import Callable
from html.parser import HTMLParser
from typing import TYPE_CHECKING, Any

from docx.image.image import Image as _DocxImage
from docx.shared import Length, Mm
from docxtpl import RichText

if TYPE_CHECKING:
    from docxtpl import DocxTemplate

# ``src`` (as it appears in an ``<img src="...">`` we're converting) -> raw image bytes, or ``None``
# if unavailable. Exceptions are treated the same as ``None`` (never let a bad image crash a render).
ImageResolver = Callable[[str], "bytes | None"]

_MAX_IMAGE_WIDTH: Length = Mm(150)

# Skip (placeholder) any single image whose bytes exceed this, so a runaway artifact can't blow up the
# render's memory footprint (belt-and-suspenders with the stat-based cap in report_docx_api). 25 MiB is
# generous for a screenshot yet bounds worst-case in-memory embedding.
_MAX_IMAGE_BYTES = 25 * 1024 * 1024

# IMPORTANT: OOXML ``<w:pStyle w:val="...">`` resolves by *styleId*, not the human display name. In the
# default python-docx style table the built-in styleIds are the spaceless form (``Heading2``,
# ``ListBullet2``, ``NoSpacing`` …), so these MUST be styleIds — emitting the spaced display name
# ("Heading 2") silently falls back to Normal. See tests/test_report_docx.py for the style assertions
# that pin this.
_HEADING_STYLES = {f"h{i}": f"Heading{i}" for i in range(1, 7)}
_LIST_STYLES = {"ul": "ListBullet", "ol": "ListNumber"}
_QUOTE_STYLE = "Quote"
_CAPTION_STYLE = "Caption"
_CODE_BLOCK_STYLE = "NoSpacing"
_MONOSPACE_FONT = "Consolas"


def _list_item_style(list_stack: list[str]) -> str:
    """The list-paragraph styleId for a ``<li>`` at the current nesting depth (capped at 3, the
    deepest built-in Word list style)."""
    depth = max(len(list_stack), 1)
    base = _LIST_STYLES.get(list_stack[-1] if list_stack else "ul", "ListBullet")
    return base if depth <= 1 else f"{base}{min(depth, 3)}"


def _run_xml(text: str, **props: Any) -> str:
    """Build a self-contained ``<w:r>...</w:r>`` (or ``<w:hyperlink>``-wrapped) fragment for ``text``.

    Delegates to :class:`RichText`'s own (well-tested) escaping/property-building rather than
    hand-rolling OOXML, and just takes the resulting ``.xml`` string.
    """
    if not text:
        return ""
    rt = RichText()
    rt.add(text, **props)
    return rt.xml


def _safe_href(url: str | None) -> str | None:
    if url and url.startswith(("http://", "https://", "mailto:")):
        return url
    return None


class _DocxHtmlWalker(HTMLParser):
    """Streams sanitized HTML into a buffer of self-contained OOXML run/paragraph fragments."""

    def __init__(self, tpl: DocxTemplate, image_resolver: ImageResolver | None):
        super().__init__(convert_charrefs=True)
        self.tpl = tpl
        self.image_resolver = image_resolver
        self.buf: list[str] = []
        self._any_paragraph_open = False
        self._mark_stack: list[tuple[str, Any]] = []  # ("bold"|"italic"|"underline"|"strike"|"font", val)
        self._link_stack: list[str | None] = []
        self._list_stack: list[str] = []  # "ul" | "ol"
        self._list_item_styles: list[str] = []  # list-paragraph styleId per currently-open <li>
        self._block_stack: list[str] = []  # "blockquote" | "figure"
        self._pre_depth = 0

    # -- paragraph management -------------------------------------------------------------------

    def _start_paragraph(self, style: str | None = None) -> None:
        prefix = "</w:p><w:p>" if self._any_paragraph_open else ""
        ppr = f'<w:pPr><w:pStyle w:val="{style}"/></w:pPr>' if style else ""
        self.buf.append(prefix + ppr)
        self._any_paragraph_open = True

    def _contextual_style(self) -> str | None:
        """The style a *plain* paragraph (a bare ``<p>``, or bare text) should take from its
        surroundings. Inside a ``<li>`` the list style wins — so ``render_html``'s real
        ``<li><p>…</p></li>`` shape puts the list style on the paragraph that actually holds the text
        (no empty list paragraph, no unstyled text). ``<blockquote>`` maps to ``Quote``."""
        if self._list_item_styles:
            return self._list_item_styles[-1]
        if "blockquote" in self._block_stack:
            return _QUOTE_STYLE
        return None

    def _ensure_paragraph(self) -> None:
        if not self._any_paragraph_open:
            self._start_paragraph(style=self._contextual_style())

    # -- inline runs ------------------------------------------------------------------------------

    def _active_run_props(self) -> dict[str, Any]:
        props: dict[str, Any] = {}
        for kind, val in self._mark_stack:
            if kind == "font":
                props["font"] = val
            else:
                props[kind] = val
        for href in reversed(self._link_stack):
            if href:
                props["url_id"] = self.tpl.build_url_id(href)
                props["underline"] = props.get("underline", "single")
                props["color"] = props.get("color", "0563C1")
                break
        return props

    def _emit_text(self, text: str) -> None:
        if not text:
            return
        self._ensure_paragraph()
        self.buf.append(_run_xml(text, **self._active_run_props()))

    def _emit_br(self) -> None:
        self._ensure_paragraph()
        self.buf.append("<w:r><w:br/></w:r>")

    # -- images -------------------------------------------------------------------------------------

    def _emit_image(self, src: str | None, alt: str | None) -> None:
        self._ensure_paragraph()
        data: bytes | None = None
        if src and self.image_resolver:
            try:
                data = self.image_resolver(src)
            except Exception:
                data = None
        if data and len(data) > _MAX_IMAGE_BYTES:
            data = None  # oversized: degrade to placeholder rather than embed a huge blob
        if data:
            try:
                self.buf.append(self._pic_xml(data))
                return
            except Exception:
                pass
        placeholder = f"[image: {alt.strip()}]" if alt and alt.strip() else "[image unavailable]"
        self.buf.append(_run_xml(placeholder, italic=True, color="8A8A8A"))

    def _pic_xml(self, data: bytes) -> str:
        part = self.tpl.current_rendering_part
        if part is None:  # pragma: no cover - defensive; caller contract requires this be set
            raise RuntimeError("tpl.current_rendering_part must be set before converting images")
        kwargs: dict[str, Any] = {}
        try:
            native_width = _DocxImage.from_blob(data).width
            if native_width > _MAX_IMAGE_WIDTH:
                kwargs["width"] = _MAX_IMAGE_WIDTH
        except Exception:
            kwargs["width"] = _MAX_IMAGE_WIDTH
        pic = part.new_pic_inline(io.BytesIO(data), **kwargs).xml
        return f"<w:r><w:drawing>{pic}</w:drawing></w:r>"

    # -- HTMLParser hooks -----------------------------------------------------------------------

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._start(tag, dict(attrs))

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._start(tag, dict(attrs))
        # self-closing tags (img, br) carry no meaningful end-tag state to pop.

    def _start(self, tag: str, attrs: dict[str, str | None]) -> None:
        try:
            self._dispatch_start(tag, attrs)
        except Exception:
            # A single malformed element must never abort the whole conversion.
            pass

    def _dispatch_start(self, tag: str, attrs: dict[str, str | None]) -> None:
        if tag == "p":
            self._start_paragraph(style=self._contextual_style())
        elif tag in _HEADING_STYLES:
            self._start_paragraph(style=_HEADING_STYLES[tag])
        elif tag == "blockquote":
            self._block_stack.append("blockquote")
        elif tag in ("ul", "ol"):
            self._list_stack.append(tag)
        elif tag == "li":
            # Don't open a paragraph here: push the list style as context so the item's inner block
            # (``render_html`` emits ``<li><p>text</p></li>``) — or bare text — carries it. This
            # avoids the empty-list-paragraph + unstyled-text bug (C2).
            self._list_item_styles.append(_list_item_style(self._list_stack))
        elif tag == "pre":
            self._pre_depth += 1
            self._start_paragraph(style=_CODE_BLOCK_STYLE)
            self._mark_stack.append(("font", _MONOSPACE_FONT))
        elif tag == "figure":
            self._block_stack.append("figure")
        elif tag == "figcaption":
            self._start_paragraph(style=_CAPTION_STYLE)
        elif tag == "img":
            self._emit_image(attrs.get("src"), attrs.get("alt"))
        elif tag == "br":
            self._emit_br()
        elif tag in ("strong", "b"):
            self._mark_stack.append(("bold", True))
        elif tag in ("em", "i"):
            self._mark_stack.append(("italic", True))
        elif tag == "code":
            self._mark_stack.append(("font", _MONOSPACE_FONT))
        elif tag in ("s", "strike", "del"):
            self._mark_stack.append(("strike", True))
        elif tag == "u":
            self._mark_stack.append(("underline", "single"))
        elif tag == "a":
            self._link_stack.append(_safe_href(attrs.get("href")))
        # span/div/table/tr/td/th/thead/tbody/unknown tags: transparent — children flow inline into
        # the current paragraph context; never raises, never drops content.

    def handle_endtag(self, tag: str) -> None:
        try:
            self._dispatch_end(tag)
        except Exception:
            pass

    def _dispatch_end(self, tag: str) -> None:
        if tag == "blockquote" and self._block_stack and self._block_stack[-1] == "blockquote":
            self._block_stack.pop()
        elif tag == "figure" and self._block_stack and self._block_stack[-1] == "figure":
            self._block_stack.pop()
        elif tag == "li" and self._list_item_styles:
            self._list_item_styles.pop()
        elif tag in ("ul", "ol") and self._list_stack:
            self._list_stack.pop()
        elif tag == "pre" and self._pre_depth > 0:
            self._pre_depth -= 1
            if self._mark_stack and self._mark_stack[-1] == ("font", _MONOSPACE_FONT):
                self._mark_stack.pop()
        elif tag in ("strong", "b") and self._mark_stack:
            self._pop_mark("bold")
        elif tag in ("em", "i") and self._mark_stack:
            self._pop_mark("italic")
        elif tag == "code" and self._mark_stack:
            self._pop_mark("font")
        elif tag in ("s", "strike", "del") and self._mark_stack:
            self._pop_mark("strike")
        elif tag == "u" and self._mark_stack:
            self._pop_mark("underline")
        elif tag == "a" and self._link_stack:
            self._link_stack.pop()

    def _pop_mark(self, kind: str) -> None:
        for i in range(len(self._mark_stack) - 1, -1, -1):
            if self._mark_stack[i][0] == kind:
                del self._mark_stack[i]
                return

    def handle_data(self, data: str) -> None:
        if self._pre_depth > 0:
            lines = data.split("\n")
            for i, line in enumerate(lines):
                if i:
                    self._emit_br()
                if line:
                    self._emit_text(line)
            return
        # Outside <pre>: collapse whitespace runs the way a browser would (our own generator never
        # emits stray whitespace between tags, but defends against hand-fed/malformed input).
        collapsed = " ".join(data.split())
        if data and data[:1].isspace() and collapsed:
            collapsed = " " + collapsed
        if data and data[-1:].isspace() and collapsed:
            collapsed = collapsed + " "
        self._emit_text(collapsed)


def html_to_richtext(
    html: str | None,
    *,
    tpl: DocxTemplate,
    image_resolver: ImageResolver | None = None,
) -> RichText:
    """Convert one sanitized HTML fragment into a :class:`RichText` for a ``{{r ... }}`` field.

    ``tpl.current_rendering_part`` must already be set (``tpl.current_rendering_part =
    tpl.docx.part``) — see the module docstring. ``image_resolver(src) -> bytes | None`` resolves an
    ``<img src="...">`` to raw bytes; returning ``None``/raising degrades gracefully to a bracketed
    placeholder. Malformed HTML never raises: worst case, the fragment renders as plain text.
    """
    rt = RichText()
    if not html or not html.strip():
        return rt
    walker = _DocxHtmlWalker(tpl, image_resolver)
    try:
        walker.feed(html)
        walker.close()
        rt.xml = "".join(walker.buf)
    except Exception:
        rt = RichText()
        rt.add(_best_effort_text(html))
    return rt


def _best_effort_text(html: str) -> str:
    """Last-resort plain-text fallback if the streaming walker itself blows up."""
    try:
        parser = _TextOnlyParser()
        parser.feed(html)
        parser.close()
        return " ".join(parser.chunks) or html
    except Exception:
        return html


class _TextOnlyParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.chunks: list[str] = []

    def handle_data(self, data: str) -> None:
        text = data.strip()
        if text:
            self.chunks.append(text)
