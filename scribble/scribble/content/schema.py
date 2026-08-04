"""ProseMirror/TipTap document schema for Scribble content blocks.

A content block is a ProseMirror ``doc`` node (dict). Standard nodes come from TipTap StarterKit; three
custom nodes are Scribble-specific:

- ``variable``      — an inline ``{{KEY}}`` reference (attrs: ``{"key": "COMPANY_NAME"}``), rendered as a
                      chip in the editor and resolved to a value at render time.
- ``inlineImage``   — an inline image referencing an ``Artifact`` (attrs: ``{"artifactId": int,
                      "alt": str, "caption": str}``).
- ``figure``        — a block figure: an image + ``figcaption`` (for figure numbering in reports).

Everything downstream (HTML render, docx render, variable resolution, Yjs later) treats this JSON as
canonical. This module is a FROZEN CONTRACT (see plans/CONTRACTS.md).
"""

from __future__ import annotations

from typing import Any

# Node type names.
DOC = "doc"
PARAGRAPH = "paragraph"
TEXT = "text"
HEADING = "heading"
BULLET_LIST = "bulletList"
ORDERED_LIST = "orderedList"
LIST_ITEM = "listItem"
BLOCKQUOTE = "blockquote"
CODE_BLOCK = "codeBlock"
HARD_BREAK = "hardBreak"
IMAGE = "image"
TABLE = "table"

# Custom nodes.
VARIABLE = "variable"
INLINE_IMAGE = "inlineImage"
FIGURE = "figure"

CUSTOM_NODES = (VARIABLE, INLINE_IMAGE, FIGURE)

# The default set of named blocks a finding/template carries.
DEFAULT_BLOCKS = ("description", "remediation", "details")


def empty_doc() -> dict[str, Any]:
    """An empty ProseMirror document."""
    return {"type": DOC, "content": []}


def paragraph(*text: str) -> dict[str, Any]:
    """A single paragraph node from plain text (convenience for seeds/tests)."""
    content = [{"type": TEXT, "text": t} for t in text if t]
    return {"type": PARAGRAPH, "content": content}


def doc_from_text(text: str) -> dict[str, Any]:
    """Wrap plain text (splitting on blank lines into paragraphs) into a doc."""
    blocks = [b.strip() for b in text.replace("\r\n", "\n").split("\n\n")]
    return {"type": DOC, "content": [paragraph(b) for b in blocks if b]}


def iter_nodes(node: dict[str, Any] | None):
    """Depth-first iteration over a node and its descendants."""
    if not node:
        return
    yield node
    for child in node.get("content", []) or []:
        yield from iter_nodes(child)


def plain_text(doc: dict[str, Any] | None) -> str:
    """Extract concatenated text (text nodes + variable keys) for previews/search."""
    out: list[str] = []
    for n in iter_nodes(doc):
        if n.get("type") == TEXT:
            out.append(n.get("text", ""))
        elif n.get("type") == VARIABLE:
            out.append("{{" + str(n.get("attrs", {}).get("key", "")) + "}}")
    return "".join(out)


def is_doc(value: Any) -> bool:
    return isinstance(value, dict) and value.get("type") == DOC
