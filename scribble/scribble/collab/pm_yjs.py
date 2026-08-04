"""ProseMirror JSON <-> pycrdt (Yjs) CRDT mapping — WS11 Phase B.

Scribble's canonical content is ProseMirror JSON (``scribble/content/schema.py``, frozen). Phase B adds
a Yjs-compatible CRDT layer so multiple people can co-edit a block live; this module is the two-way
mapping between the two representations, modeled after how ``y-prosemirror`` maps a ProseMirror doc onto
a ``Y.XmlFragment`` (so the *wire representation* here is compatible with a real Yjs client, even though
Scribble currently ships only a documented client stub — see ``scribble/static/collab.js``).

Mapping (mirrors y-prosemirror's actual approach):
- The doc's top-level ``content`` array becomes the children of a root ``XmlFragment``.
- Each block node becomes an ``XmlElement`` whose ``tag`` is the node type and whose ``attributes`` are
  the node's ``attrs`` dict (Yjs attributes accept arbitrary JSON-ish values, not just strings).
- A node that holds *inline* content (``paragraph``/``heading``/``codeBlock``/``figure``) gets exactly one
  ``XmlText`` child: plain text runs are inserted with mark-derived formatting attributes
  (``{"bold": True, "link": {"href": ...}}``); non-text inline leaves (``hardBreak``/``image``/
  ``variable``/``inlineImage``) are inserted as *embeds* (``insert_embed``) carrying their type + attrs,
  exactly as y-prosemirror embeds non-text ProseMirror nodes inside a ``Y.XmlText``.
- A node that holds *block* content (``bulletList``/``orderedList``/``listItem``/``blockquote``, and the
  root ``doc``) recurses: each child becomes its own nested ``XmlElement``.
- Unknown/foreign node types never drop content (same philosophy as ``content/render_html.py`` and
  ``static/editor.js``): they're classified as inline- or block-holding by inspecting their first child.

Yjs/JSON numbers round-trip as floats (``42`` -> ``42.0``); :func:`_normalize` converts integral floats
back to ``int`` so reconstructed docs compare equal to hand-written ProseMirror JSON fixtures.
"""

from __future__ import annotations

from typing import Any

import pycrdt as Y

from scribble.content import schema

# Root key for the document's XmlFragment within a pycrdt.Doc.
ROOT_KEY = "prosemirror"

# Node types that hold *inline* content directly (get a single XmlText child).
_INLINE_CONTENT_TYPES = {schema.PARAGRAPH, schema.HEADING, schema.CODE_BLOCK, schema.FIGURE}

# Node types that hold *block* content (nested XmlElements, no XmlText).
_BLOCK_CONTENT_TYPES = {schema.BULLET_LIST, schema.ORDERED_LIST, schema.LIST_ITEM, schema.BLOCKQUOTE}

# Inline leaf node types: never containers themselves, always a run within an XmlText.
_INLINE_LEAF_TYPES = {
    schema.TEXT,
    schema.HARD_BREAK,
    schema.IMAGE,
    schema.VARIABLE,
    schema.INLINE_IMAGE,
}

# Leaf node types that carry NO child content when they surface as their own ``XmlElement`` (i.e. as a
# direct *block* child rather than inline inside an ``XmlText``). These must never gain a spurious
# ``content: []`` on the round trip, or merely opening+closing a collab session with zero edits would
# rewrite ``content_json`` (the "N1" review finding). ``TEXT`` is excluded — a bare text node is only
# ever produced as a run inside an ``XmlText``, never as a standalone block element. Inline occurrences
# of these types are handled as embeds by :func:`_populate_inline`, not here.
_LEAF_TYPES = {schema.HARD_BREAK, schema.IMAGE, schema.VARIABLE, schema.INLINE_IMAGE}


def _normalize(value: Any) -> Any:
    """Undo Yjs's float64-only JSON numbers (``42.0`` -> ``42``) recursively."""
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, dict):
        return {k: _normalize(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_normalize(v) for v in value]
    return value


def _canonical_marks(marks: list[dict]) -> list[dict]:
    """Sort marks by type for a deterministic, comparable representation.

    We control both encode (:func:`_populate_inline`) and decode (:func:`_read_inline`) sides of this
    mapping, so canonicalizing mark order here makes ``ydoc_to_doc(doc_to_ydoc(x)) == x`` hold regardless
    of the input's original mark ordering (dict/set iteration order is not guaranteed to survive a CRDT
    round trip)."""
    return sorted(marks, key=lambda m: m["type"])


def _is_inline_holder(node_type: str, content: list[dict]) -> bool:
    if node_type in _INLINE_CONTENT_TYPES:
        return True
    if node_type in _BLOCK_CONTENT_TYPES or node_type == schema.DOC:
        return False
    # Unknown/foreign node: guess from the first child so we never crash on forward-compat content.
    return bool(content) and content[0].get("type") in _INLINE_LEAF_TYPES


def append_node(parent_children, node: dict) -> None:
    """Build an ``XmlElement`` for ``node`` and append it to ``parent_children``.

    pycrdt requires a shared type to be *integrated* into the document (i.e. already attached to a
    parent that's part of the doc tree) before its own ``.children``/``.attributes`` views are usable —
    so we must append the (empty) element to its parent first, then populate it, not build a fully
    populated subtree and append it in one shot.
    """
    node_type = node.get("type", "unknown")
    attrs = dict(node.get("attrs") or {})
    el = Y.XmlElement(node_type, attrs)
    parent_children.append(el)  # integrates el into the doc
    if node_type in _LEAF_TYPES:
        return  # leaf: attributes only, never child content (see _LEAF_TYPES / N1)
    content = node.get("content") or []
    if _is_inline_holder(node_type, content):
        text = Y.XmlText()
        el.children.append(text)  # el is now integrated; this works
        _populate_inline(text, content)
    else:
        for child in content:
            append_node(el.children, child)


def _populate_inline(xmltext, content: list[dict]) -> None:
    index = 0
    for node in content:
        node_type = node.get("type")
        if node_type == schema.TEXT:
            text = node.get("text", "")
            marks = node.get("marks") or []
            fmt = {m["type"]: (m.get("attrs") if m.get("attrs") else True) for m in marks}
            xmltext.insert(index, text, fmt)
            index += len(text)
        else:
            attrs = dict(node.get("attrs") or {})
            embed = {"type": node_type, "attrs": attrs}
            # Explicit ``{}`` attrs (never ``None``) — an embed inserted without an explicit attrs dict
            # can silently inherit the format of a neighboring run, which would corrupt marks.
            xmltext.insert_embed(index, embed, {})
            index += 1


def _xml_element_to_node(el) -> dict:
    node_type = el.tag
    attrs = _normalize(dict(el.attributes))
    node: dict[str, Any] = {"type": node_type}
    if attrs:
        node["attrs"] = attrs
    if node_type in _LEAF_TYPES:
        return node  # leaf: no content key (mirrors append_node; avoids no-op round-trip churn)
    children = list(el.children)
    if len(children) == 1 and isinstance(children[0], Y.XmlText):
        node["content"] = _read_inline(children[0])
    else:
        node["content"] = [_xml_element_to_node(c) for c in children]
    return node


def _read_inline(xmltext) -> list[dict]:
    out: list[dict] = []
    for value, fmt in xmltext.diff():
        if isinstance(value, str):
            text_node: dict[str, Any] = {"type": schema.TEXT, "text": value}
            if fmt:
                marks = [
                    {"type": k, **({"attrs": _normalize(v)} if v is not True else {})}
                    for k, v in fmt.items()
                ]
                text_node["marks"] = _canonical_marks(marks)
            out.append(text_node)
        else:
            embed = value if isinstance(value, dict) else {}
            node_type = embed.get("type", "unknown")
            attrs = _normalize(embed.get("attrs") or {})
            leaf: dict[str, Any] = {"type": node_type}
            if attrs:
                leaf["attrs"] = attrs
            out.append(leaf)
    return out


def doc_to_ydoc(pm_doc: dict | None, ydoc: Y.Doc, *, root_key: str = ROOT_KEY) -> None:
    """Populate ``ydoc``'s root ``XmlFragment`` from a ProseMirror JSON doc.

    Intended for a brand-new, empty ``ydoc`` (e.g. seeding a fresh CRDT room from a finding's existing
    ``content_json[block]`` before anyone has connected). Must run inside/establish its own transaction.
    """
    frag = ydoc.get(root_key, type=Y.XmlFragment)
    with ydoc.transaction():
        for child in (pm_doc or {}).get("content") or []:
            append_node(frag.children, child)


def ydoc_to_doc(ydoc: Y.Doc, *, root_key: str = ROOT_KEY) -> dict:
    """Render ``ydoc``'s root ``XmlFragment`` back to a ProseMirror JSON doc."""
    frag = ydoc.get(root_key, type=Y.XmlFragment)
    return {"type": schema.DOC, "content": [_xml_element_to_node(c) for c in frag.children]}


def normalize_doc(pm_doc: dict | None) -> dict:
    """Round-trip ``pm_doc`` through a scratch ``Doc`` to its canonical CRDT-rendered shape.

    Used to compare a finding's stored ``content_json[block]`` against a CRDT room's rendered content
    for *semantic* divergence, ignoring benign shape differences the round trip normalizes (adjacent
    same-mark run coalescing, ``42.0``->``42``, mark ordering — see ``ydoc_to_doc``/``_canonical_marks``).
    Comparing normalized-vs-normalized avoids falsely flagging such churn as a real out-of-band edit
    (the "C2 freshness" check in ``scribble/collab/crdt.py``)."""
    scratch = Y.Doc()
    doc_to_ydoc(pm_doc, scratch)
    return ydoc_to_doc(scratch)
