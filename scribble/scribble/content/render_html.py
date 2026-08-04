"""ProseMirror JSON -> sanitized HTML.

A minimal but real walker covering the StarterKit nodes + Scribble's custom nodes. WS7 extends node
coverage (tables, etc.); the dispatch shape is stable. Output is sanitized with ``nh3`` so it is safe to
embed in reports and the app. Variables and inline-image URLs are resolved via injected callbacks so this
module stays free of DB/engagement knowledge.
"""

from __future__ import annotations

from collections.abc import Callable
from html import escape

import nh3

from scribble.content import schema

# Marks -> (open, close) HTML.
_MARKS = {
    "bold": ("<strong>", "</strong>"),
    "italic": ("<em>", "</em>"),
    "code": ("<code>", "</code>"),
    "strike": ("<s>", "</s>"),
    "underline": ("<u>", "</u>"),
}

VarResolver = Callable[[str], str]
ArtifactUrl = Callable[[int], str]


def _text_node(node: dict) -> str:
    text = node.get("text", "")
    html = escape(text)
    for mark in node.get("marks", []) or []:
        mtype = mark.get("type")
        if mtype == "link":
            href = escape(mark.get("attrs", {}).get("href", "#"), quote=True)
            html = f'<a href="{href}">{html}</a>'
        elif mtype in _MARKS:
            open_, close_ = _MARKS[mtype]
            html = f"{open_}{html}{close_}"
    return html


def _children(node: dict, **kw) -> str:
    return "".join(_render_node(c, **kw) for c in node.get("content", []) or [])


def _render_node(node: dict, *, resolve_var: VarResolver | None, artifact_url: ArtifactUrl | None) -> str:
    t = node.get("type")
    kw = {"resolve_var": resolve_var, "artifact_url": artifact_url}

    if t == schema.TEXT:
        return _text_node(node)
    if t == schema.PARAGRAPH:
        return f"<p>{_children(node, **kw)}</p>"
    if t == schema.HEADING:
        level = min(max(int(node.get("attrs", {}).get("level", 2)), 1), 6)
        return f"<h{level}>{_children(node, **kw)}</h{level}>"
    if t == schema.BULLET_LIST:
        return f"<ul>{_children(node, **kw)}</ul>"
    if t == schema.ORDERED_LIST:
        return f"<ol>{_children(node, **kw)}</ol>"
    if t == schema.LIST_ITEM:
        return f"<li>{_children(node, **kw)}</li>"
    if t == schema.BLOCKQUOTE:
        return f"<blockquote>{_children(node, **kw)}</blockquote>"
    if t == schema.CODE_BLOCK:
        return f"<pre><code>{_children(node, **kw)}</code></pre>"
    if t == schema.HARD_BREAK:
        return "<br/>"
    if t == schema.IMAGE:
        src = escape(node.get("attrs", {}).get("src", ""), quote=True)
        alt = escape(node.get("attrs", {}).get("alt", ""), quote=True)
        return f'<img src="{src}" alt="{alt}"/>'
    if t == schema.VARIABLE:
        key = node.get("attrs", {}).get("key", "")
        value = resolve_var(key) if resolve_var else "{{" + key + "}}"
        return escape(value)
    if t == schema.INLINE_IMAGE:
        attrs = node.get("attrs", {})
        aid = attrs.get("artifactId")
        src = escape(artifact_url(aid), quote=True) if (artifact_url and aid is not None) else ""
        alt = escape(attrs.get("alt", ""), quote=True)
        return f'<img class="artifact" src="{src}" alt="{alt}"/>'
    if t == schema.FIGURE:
        inner = _children(node, **kw)
        cap = escape(node.get("attrs", {}).get("caption", ""))
        cap_html = f"<figcaption>{cap}</figcaption>" if cap else ""
        return f"<figure>{inner}{cap_html}</figure>"
    if t == schema.DOC:
        return _children(node, **kw)
    # Unknown node: render its children so we never drop content.
    return _children(node, **kw)


_ALLOWED_TAGS = {
    "p", "br", "strong", "em", "code", "s", "u", "a", "h1", "h2", "h3", "h4", "h5", "h6",
    "ul", "ol", "li", "blockquote", "pre", "img", "figure", "figcaption", "span", "div", "table",
    "thead", "tbody", "tr", "td", "th",
}

_ALLOWED_ATTRS = {
    # nh3 manages "rel" on <a> itself (link_rel), so it must not appear here.
    "a": {"href", "title"},
    "img": {"src", "alt", "class", "width", "height"},
    "span": {"class"},
    "div": {"class"},
    "td": {"colspan", "rowspan"},
    "th": {"colspan", "rowspan"},
}


def render_block(
    doc: dict | None,
    *,
    resolve_var: VarResolver | None = None,
    artifact_url: ArtifactUrl | None = None,
) -> str:
    """Render one content block (a ProseMirror doc) to sanitized HTML.

    NOTE: uses nh3's default URL schemes (http/https/mailto/relative). The self-contained report
    inliner (WS7) that embeds artifact bytes as ``data:`` URIs must sanitize with ``data`` allowed
    separately — do not widen the schemes here (keeps app-surface HTML tight)."""
    if not doc:
        return ""
    raw = _render_node(doc, resolve_var=resolve_var, artifact_url=artifact_url)
    return nh3.clean(raw, tags=_ALLOWED_TAGS, attributes=_ALLOWED_ATTRS)
