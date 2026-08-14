"""Sanitize untrusted ProseMirror JSON before it is stored and later rendered.

A write-scoped PAT can POST arbitrary ``content_json`` — the ``{block_name: prosemirror_doc}`` mapping a
finding/template carries — which Scribble stores verbatim and later feeds to ``render_html`` to build an
HTML report a human opens in a browser. Untrusted JSON is therefore a *stored-XSS* vector:

- a node of a type Scribble does not emit (a TipTap ``rawHtml`` node, an unknown future node, or an
  attacker-invented ``"html"``/``"script"`` node) can smuggle markup;
- a ``link`` mark whose ``href`` is ``javascript:…`` (or ``data:``/``vbscript:``) executes on click;
- unknown attributes can carry event handlers or embedded HTML.

This module strips a document down to a small, *closed allowlist* of node types, marks and attributes.
Anything else is dropped whole — a disallowed node is removed with its subtree (children are NOT hoisted;
the simplest correct behaviour), a disallowed mark is removed while the underlying text is kept, and every
attribute that is not explicitly permitted is discarded. Nothing here executes, evaluates, or fetches; it
only rebuilds a fresh, trusted dict.

The output shape mirrors :mod:`scribble.content.schema` exactly — a ``doc`` node whose ``content`` is a
list of nodes, ``text`` nodes carrying an optional ``marks`` list, ``heading`` keeping only a clamped
``level`` — so a sanitized document (like one from ``schema.doc_from_text``) feeds
``scribble.content.render_html.render_block`` unchanged. ``render_block`` re-sanitizes with ``nh3`` as a
second, independent layer; this module is the primary gate applied at *write* time so malicious JSON is
never persisted in the first place.
"""

from __future__ import annotations

from typing import Any

from scribble.content import schema

# Node types permitted in stored content. Every other type — raw-HTML nodes, custom Scribble nodes we do
# not trust from an untrusted caller (variable/inlineImage/figure carry references resolved at render
# time), and any unknown/future type — is dropped whole, subtree included.
ALLOWED_NODE_TYPES: frozenset[str] = frozenset(
    {
        schema.DOC,  # "doc"
        schema.PARAGRAPH,  # "paragraph"
        schema.HEADING,  # "heading"
        schema.BULLET_LIST,  # "bulletList"
        schema.ORDERED_LIST,  # "orderedList"
        schema.LIST_ITEM,  # "listItem"
        schema.CODE_BLOCK,  # "codeBlock"
        schema.TEXT,  # "text"
        schema.HARD_BREAK,  # "hardBreak"
    }
)

# Inline marks a text node may carry. Everything else (strike, underline, textStyle, a smuggled mark …)
# is dropped while the text it decorated is preserved.
ALLOWED_MARKS: frozenset[str] = frozenset({"bold", "italic", "code", "link"})

# A ``link`` mark's href must begin with one of these. This is an ALLOWLIST, not a denylist: a value the
# check does not recognise — ``javascript:``, ``data:``, ``vbscript:``, ``mailto:``, a relative path, or
# any control-character-prefixed evasion of a would-be denylist — drops the mark and keeps the text.
_ALLOWED_LINK_PREFIXES: tuple[str, ...] = ("http://", "https://")

# Container node types (allowlisted, minus the two leaf types) always get a ``content`` list.
_LEAF_NODE_TYPES: frozenset[str] = frozenset({schema.TEXT, schema.HARD_BREAK})

# Maximum node nesting the walker will descend. ``content_json`` arrives from a write-scoped PAT, and
# ``json.loads`` happily accepts a document nested hundreds of levels deep (bulletList→listItem→…); an
# uncapped recursion here — and in the downstream ``render_html`` walker — would blow Python's recursion
# limit and 500 the write. A real finding body is a handful of levels deep, so a generous cap drops only
# adversarial input, as a clean removal rather than an unhandled error (INV-INPUT-02: bounded parse).
_MAX_DEPTH = 64


def _clean_heading_attrs(attrs: Any) -> dict[str, int]:
    """Keep only a clamped integer ``level`` (1..6); discard every other heading attribute."""
    level: Any = attrs.get("level", 1) if isinstance(attrs, dict) else 1
    try:
        level = int(level)
    except (TypeError, ValueError, OverflowError):
        # OverflowError is not hypothetical: Python's json accepts the non-standard `Infinity` /
        # `-Infinity` literals, and `int(float("inf"))` raises OverflowError — NOT ValueError. A write
        # token posting {"type":"heading","attrs":{"level":Infinity}} would otherwise 500 the request
        # from inside the module whose whole job is handling untrusted JSON safely.
        level = 1
    return {"level": min(max(level, 1), 6)}


def _clean_marks(marks: Any) -> list[dict[str, Any]]:
    """Return the allowlisted marks from a text node's ``marks``, dropping the rest.

    ``bold``/``italic``/``code`` are kept with no attributes. ``link`` is kept only when its ``href`` is a
    string beginning with an allowed scheme, and then only the ``href`` attribute survives.
    """
    out: list[dict[str, Any]] = []
    if not isinstance(marks, list):
        return out
    for mark in marks:
        if not isinstance(mark, dict):
            continue
        mtype = mark.get("type")
        if mtype not in ALLOWED_MARKS:
            continue
        if mtype == "link":
            attrs = mark.get("attrs")
            href = attrs.get("href") if isinstance(attrs, dict) else None
            if not isinstance(href, str) or not href.lower().startswith(_ALLOWED_LINK_PREFIXES):
                continue  # drop the mark; the text node itself is still kept by the caller
            out.append({"type": "link", "attrs": {"href": href}})
        else:
            out.append({"type": mtype})
    return out


def _clean_node(node: Any, depth: int = 0) -> dict[str, Any] | None:
    """Return a sanitized copy of one node, or ``None`` if the node must be dropped entirely."""
    if not isinstance(node, dict):
        return None
    if depth > _MAX_DEPTH:
        return None  # over-deep subtree: drop it whole rather than recurse into a RecursionError (500)
    ntype = node.get("type")
    if ntype not in ALLOWED_NODE_TYPES:
        return None  # disallowed type: drop the node and its whole subtree (no hoisting)

    clean: dict[str, Any] = {"type": ntype}

    if ntype == schema.TEXT:
        text = node.get("text", "")
        if not isinstance(text, str):
            return None  # a malformed text node with no string payload is worthless
        clean["text"] = text
        marks = _clean_marks(node.get("marks"))
        if marks:
            clean["marks"] = marks
        return clean

    if ntype == schema.HARD_BREAK:
        return clean

    if ntype == schema.HEADING:
        clean["attrs"] = _clean_heading_attrs(node.get("attrs"))

    # Container node: recurse into children, keeping only those that survive sanitization. Every other
    # attribute (raw-HTML payloads, unknown keys) is silently discarded by never being copied across.
    children = node.get("content")
    cleaned_children: list[dict[str, Any]] = []
    if isinstance(children, list):
        for child in children:
            cleaned_child = _clean_node(child, depth + 1)
            if cleaned_child is not None:
                cleaned_children.append(cleaned_child)
    clean["content"] = cleaned_children
    return clean


def sanitize_prosemirror(doc: dict[str, Any]) -> dict[str, Any]:
    """Return a sanitized *copy* of a single ProseMirror ``doc``.

    Keeps only the node types in :data:`ALLOWED_NODE_TYPES`, the marks in :data:`ALLOWED_MARKS`, and the
    minimal per-node attributes (a clamped heading ``level``; an http(s) link ``href``). The input is
    never mutated — every node in the result is freshly built. Anything whose top-level shape is not a
    ``doc`` node is replaced with an empty document, so an untrusted caller cannot smuggle a non-``doc``
    root past the walker.
    """
    if not isinstance(doc, dict) or doc.get("type") != schema.DOC:
        return schema.empty_doc()
    cleaned = _clean_node(doc)
    # _clean_node returns a dict for an allowlisted "doc" node; the guard is defensive.
    return cleaned if cleaned is not None else schema.empty_doc()


def sanitize_content_json(blocks: dict[str, Any]) -> dict[str, Any]:
    """Apply :func:`sanitize_prosemirror` to every block of a ``content_json`` mapping.

    ``content_json`` is ``{block_name: prosemirror_doc}``. Each block is sanitized independently; a
    non-mapping input yields an empty mapping. Block names are coerced to ``str`` so the returned keys are
    always JSON-safe.
    """
    if not isinstance(blocks, dict):
        return {}
    return {str(name): sanitize_prosemirror(doc) for name, doc in blocks.items()}
