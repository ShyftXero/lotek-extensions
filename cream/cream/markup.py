"""A deliberately tiny markup subset for scoping prose — escape first, format second.

Line-item detail and the notes/terms blocks want *some* structure: a bullet list of in-scope ranges, a
bold caveat, a monospaced CIDR. They do not want a markdown library, and they emphatically do not want
raw HTML: this text is rendered into a PDF by a full CSS engine and into a live browser preview, so an
``<img src=x onerror=…>`` or an ``<iframe>`` typed into a scope description would be a stored-XSS in the
preview and an outbound fetch from the PDF renderer.

So the order is the security property: **``html.escape`` the whole string, then apply a handful of
regexes to the already-escaped text.** No tag the author typed can survive, because by the time any
formatting runs there are no ``<`` characters left — only ``&lt;``. Adding a feature here means adding a
regex over escaped text; it can never mean "let this tag through".

Supported: blank-line paragraphs, ``- `` bullet lists, ``**bold**``, ``*italic*``, ``` `code` ```, and a
single newline as a line break.
"""

from __future__ import annotations

import re
from html import escape

_BOLD = re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*", re.DOTALL)
_ITALIC = re.compile(r"(?<![\w*])\*(?=\S)([^*\n]+?)(?<=\S)\*(?![\w*])")
_CODE = re.compile(r"`([^`\n]+)`")


def _inline(text: str) -> str:
    """Inline formatting over ALREADY-ESCAPED text.

    Code spans are lifted out to placeholders before the emphasis passes and put back afterwards, so
    ``**`` inside backticks stays literal — the point of a code span. The placeholder uses NUL, which
    ``html.escape`` cannot produce and no author can type into a form field.
    """
    spans: list[str] = []

    def _stash(match: re.Match[str]) -> str:
        spans.append(match.group(1))
        return f"\x00{len(spans) - 1}\x00"

    out = _CODE.sub(_stash, text)
    out = _BOLD.sub(lambda m: f"<strong>{m.group(1)}</strong>", out)
    out = _ITALIC.sub(lambda m: f"<em>{m.group(1)}</em>", out)
    for index, span in enumerate(spans):
        out = out.replace(f"\x00{index}\x00", f"<code>{span}</code>")
    return out


def render_markup(text: str | None) -> str:
    """Render the restricted subset to HTML. ``None``/blank -> ``""``.

    The return value is trusted HTML *because this function produced it* — every caller marks it safe in
    the template, so nothing else may be added to the output that did not come through ``escape``.
    """
    if not text or not text.strip():
        return ""
    blocks: list[str] = []
    bullets: list[str] = []
    para: list[str] = []

    def _flush_bullets() -> None:
        if bullets:
            blocks.append("<ul>" + "".join(f"<li>{b}</li>" for b in bullets) + "</ul>")
            bullets.clear()

    def _flush_para() -> None:
        if para:
            blocks.append("<p>" + "<br>".join(para) + "</p>")
            para.clear()

    for raw_block in re.split(r"\n\s*\n", escape(text.strip())):
        if not raw_block.strip():
            continue
        for line in raw_block.split("\n"):
            stripped = line.strip()
            if stripped.startswith("- ") or stripped.startswith("* "):
                _flush_para()
                bullets.append(_inline(stripped[2:].strip()))
            elif stripped:
                _flush_bullets()
                para.append(_inline(stripped))
        _flush_para()
        _flush_bullets()
    return "".join(blocks)


def plain(text: str | None) -> str:
    """Escaped one-liner — no block structure, newlines collapsed. For table cells and headings."""
    if not text:
        return ""
    return _inline(escape(" ".join(text.split())))
