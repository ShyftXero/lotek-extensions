"""Server-side ProseMirror-JSON -> sanitized HTML for bugreport report bodies.

Bugreport adopts the shared kit reporting editor (``lotek_kit/static/reporting-editor.js``), which
reads/writes a ProseMirror-JSON document. The NODE/MARK schema is the frozen contract declared at the
top of that JS file (and mirrored, for scribble, in ``scribble/content/schema.py`` — which bugreport
must NOT import: extensions never import each other; this could move into the kit later, once a second
consumer needs it, per the kit's admission rule).

This module renders that JSON back to HTML for the read view. It is a sanitizer **by construction**:
it builds the markup from the trusted document, HTML-escaping every text and attribute value and
emitting only a fixed allowlist of tags. So a hostile body cannot inject script or markup, an inline
image can only resolve to one of THIS report's own artifacts (never an arbitrary external URL), and a
link is dropped unless its scheme is http/https/mailto. Recursion is depth-bounded so a maliciously
nested document cannot exhaust the stack.
"""

from __future__ import annotations

import html
import json
from collections.abc import Callable
from typing import Any

from markupsafe import Markup

#: Marks the editor emits -> the inline tag that carries them. `link` is handled separately (it needs a
#: scheme-checked href). Anything not here is dropped, keeping only its text.
_MARK_TAG = {"bold": "strong", "italic": "em", "code": "code", "strike": "s", "underline": "u"}

#: A link href is rendered only when it starts with one of these. Everything else (notably `javascript:`
#: and `data:`) is dropped — the link text is kept, the dangerous href is not.
_SAFE_LINK_SCHEMES = ("http://", "https://", "mailto:")

#: Cap nesting so a document that nests lists/quotes thousands deep cannot blow the stack (a report body
#: is attacker-influenced once the machine API can author one). Beyond this, deeper blocks are dropped.
_MAX_DEPTH = 40


def _esc(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _render_inline(nodes: list[dict] | None, out: list[str], artifact_url: Callable[[Any], str] | None) -> None:
    for node in nodes or []:
        if not isinstance(node, dict):
            continue
        t = node.get("type")
        if t == "text":
            frag = _esc(node.get("text", ""))
            for mark in node.get("marks") or []:
                mt = mark.get("type") if isinstance(mark, dict) else None
                if mt == "link":
                    href = str((mark.get("attrs") or {}).get("href", ""))
                    if href.lower().startswith(_SAFE_LINK_SCHEMES):
                        frag = f'<a href="{_esc(href)}" rel="noopener noreferrer nofollow">{frag}</a>'
                    # else: unsafe scheme -> keep the text, drop the href
                elif mt in _MARK_TAG:
                    tag = _MARK_TAG[mt]
                    frag = f"<{tag}>{frag}</{tag}>"
            out.append(frag)
        elif t == "hardBreak":
            out.append("<br>")
        elif t == "variable":
            out.append(_esc("{{" + str((node.get("attrs") or {}).get("key", "")) + "}}"))
        elif t == "inlineImage":
            attrs = node.get("attrs") or {}
            aid = attrs.get("artifactId")
            if aid is not None and artifact_url is not None:
                out.append(
                    f'<img class="br-inline-image" src="{_esc(artifact_url(aid))}" alt="{_esc(attrs.get("alt", ""))}">'
                )
        elif t == "image":
            # A generic image node is rendered only for a SAME-ORIGIN relative src (i.e. one of our own
            # artifact URLs) — never an arbitrary external URL (tracking pixel / SSRF-adjacent).
            attrs = node.get("attrs") or {}
            src = str(attrs.get("src", ""))
            # A same-origin absolute path only. `//host/x` also starts with "/" but is a
            # PROTOCOL-RELATIVE url that loads from `host` — external, so it is rejected here.
            if src.startswith("/") and not src.startswith("//"):
                out.append(f'<img src="{_esc(src)}" alt="{_esc(attrs.get("alt", ""))}">')
        # any other inline node type -> dropped (never trusted verbatim)


def _render_block(node: dict, out: list[str], artifact_url: Callable[[Any], str] | None, depth: int) -> None:
    if not isinstance(node, dict) or depth > _MAX_DEPTH:
        return
    t = node.get("type")
    if t == "paragraph":
        out.append("<p>")
        _render_inline(node.get("content"), out, artifact_url)
        out.append("</p>")
    elif t == "heading":
        try:
            level = max(1, min(6, int((node.get("attrs") or {}).get("level", 2))))
        except (TypeError, ValueError):
            level = 2
        out.append(f"<h{level}>")
        _render_inline(node.get("content"), out, artifact_url)
        out.append(f"</h{level}>")
    elif t in ("bulletList", "orderedList"):
        tag = "ul" if t == "bulletList" else "ol"
        out.append(f"<{tag}>")
        for li in node.get("content") or []:
            if isinstance(li, dict) and li.get("type") == "listItem":
                out.append("<li>")
                for block in li.get("content") or []:
                    _render_block(block, out, artifact_url, depth + 1)
                out.append("</li>")
        out.append(f"</{tag}>")
    elif t == "blockquote":
        out.append("<blockquote>")
        for block in node.get("content") or []:
            _render_block(block, out, artifact_url, depth + 1)
        out.append("</blockquote>")
    elif t == "codeBlock":
        text = "".join(
            c.get("text", "") for c in node.get("content") or [] if isinstance(c, dict) and c.get("type") == "text"
        )
        out.append(f"<pre><code>{_esc(text)}</code></pre>")
    elif t == "figure":
        out.append("<figure>")
        _render_inline(node.get("content"), out, artifact_url)
        caption = (node.get("attrs") or {}).get("caption", "")
        if caption:
            out.append(f"<figcaption>{_esc(caption)}</figcaption>")
        out.append("</figure>")
    else:
        # Unknown/foreign block: best-effort render its inline content as a paragraph, never drop text.
        out.append("<p>")
        _render_inline(node.get("content"), out, artifact_url)
        out.append("</p>")


def _as_doc(body: Any) -> dict | None:
    """A stored body is EITHER ProseMirror-JSON (new) or plain text (legacy / standalone). Return the
    doc dict when the value is a document, else ``None`` (meaning: treat it as plain text)."""
    if isinstance(body, dict):
        return body if body.get("type") == "doc" else None
    if isinstance(body, str) and body.strip().startswith("{"):
        try:
            parsed = json.loads(body)
        except (ValueError, TypeError):
            return None
        if isinstance(parsed, dict) and parsed.get("type") == "doc":
            return parsed
    return None


def render_body(body: Any, artifact_url: Callable[[Any], str] | None = None) -> Markup:
    """Render a stored report body to safe HTML. Accepts a ProseMirror-JSON doc (dict or JSON string) or
    a legacy plain-text body (escaped, with blank lines -> paragraphs and single newlines -> ``<br>``)."""
    doc = _as_doc(body)
    if doc is None:
        text = body if isinstance(body, str) else ""
        paras = [p for p in text.split("\n\n") if p.strip()]
        rendered = "".join(
            "<p>" + "<br>".join(_esc(line) for line in para.split("\n")) + "</p>" for para in paras
        )
        return Markup(rendered or "<p></p>")
    out: list[str] = []
    for node in doc.get("content") or []:
        _render_block(node, out, artifact_url, 0)
    return Markup("".join(out) or "<p></p>")


def text_to_doc(text: str) -> dict:
    """A legacy plain-text body -> the minimal ProseMirror doc the editor can load, so an existing
    report opens in the rich editor with its text intact (blank line -> paragraph, newline -> break)."""
    content: list[dict] = []
    for para in (text or "").split("\n\n"):
        lines = para.split("\n")
        inline: list[dict] = []
        for i, line in enumerate(lines):
            if line:
                inline.append({"type": "text", "text": line})
            if i < len(lines) - 1:
                inline.append({"type": "hardBreak"})
        content.append({"type": "paragraph", "content": inline})
    return {"type": "doc", "content": content or [{"type": "paragraph", "content": []}]}


def body_to_doc(body: Any) -> dict:
    """The document to hand the editor as its initial content: the stored JSON doc if it is one, else a
    legacy plain-text body converted with :func:`text_to_doc`."""
    doc = _as_doc(body)
    return doc if doc is not None else text_to_doc(body if isinstance(body, str) else "")
