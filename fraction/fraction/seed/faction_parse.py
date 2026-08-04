"""HTML -> ProseMirror-JSON parser for FACTION seed content (WS12).

The FACTION export embeds a small, fixed subset of raw HTML inside JSON string fields, and further
chops the ``Description`` field into ``# Description`` / ``# Impact`` / ``# Replication Steps``
sections by a plain-text convention (not real Markdown, not real HTML headings). Sprint 0's importer
(``fraction/seed/loader.py``) wrapped that whole blob as literal text via ``schema.doc_from_text``, so
reports showed literal ``# Description <p>...</p>`` instead of real paragraphs/lists. This module turns
that source format into real ``fraction.content.schema`` ProseMirror doc JSON.

Design notes:
- Uses stdlib ``html.parser.HTMLParser`` (no new dependency) to build a small in-memory tree, tolerant
  of mismatched/unclosed tags (pops the open-tag stack to the nearest matching ancestor; ignores a
  stray end tag with no matching ancestor at all), then walks that tree into the schema's node
  vocabulary. Unknown tags never crash and never silently drop content: an unknown *block-level*
  wrapper promotes its children into the surrounding block context; an unknown *inline* wrapper's
  children render as plain runs (marks it doesn't understand are just not applied) -- i.e. "unknown
  tags degrade to their text."
- Token normalization (``normalize_tokens``): FACTION's ``{{client}}`` / bare ``CLIENT`` both become
  Fraction's real ``{{COMPANY_NAME}}`` builtin. FACTION's ``{{.foo}}`` syntax (e.g. ``{{.pass_pol}}``)
  is an internal FACTION cross-reference token, not valid Jinja -- left verbatim it would (a) render
  literally in reports and (b) poison ``templating.resolver.resolve_text``, which gives up and returns
  a whole string *unresolved* the moment it contains any invalid ``{{ }}`` expression, silently hiding
  a real ``{{COMPANY_NAME}}`` token that happens to share the same text run. We normalize it to a plain
  bracketed literal (``[pass_pol]``): human-readable, inert to the template engine, and clearly a
  "fill this in" marker for a report author. Normalization is skipped inside ``<pre>``/``<code>`` text
  so real code samples are never rewritten (no seed record currently needs this, but it's the safe
  default for future imports).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any

from fraction.content import schema

# ---------------------------------------------------------------------------
# Token normalization
# ---------------------------------------------------------------------------

_CLIENT_BRACE_RE = re.compile(r"\{\{\s*client\s*\}\}", re.IGNORECASE)
_CLIENT_WORD_RE = re.compile(r"\bCLIENT\b")
_FOREIGN_TOKEN_RE = re.compile(r"\{\{\s*\.([A-Za-z0-9_]+)\s*\}\}")
# FACTION's stock prose hardcodes the testing firm's name ("NemesisGroup", no space -- the spaced
# "Nemesis Group" is scrubbed upstream in convert_findings.py, but the raw export carries the bare
# form). It's the assessor, not the client, so it must NOT become {{COMPANY_NAME}}; neutralize it to a
# generic label so seeded templates don't ship a stranger's brand name.
_FIRM_RE = re.compile(r"\bNemesisGroup\b")


def normalize_tokens(text: str) -> str:
    """FACTION -> Fraction token normalization. See module docstring for the rationale."""
    if not text:
        return text
    text = _CLIENT_BRACE_RE.sub("{{COMPANY_NAME}}", text)
    text = _CLIENT_WORD_RE.sub("{{COMPANY_NAME}}", text)
    text = _FOREIGN_TOKEN_RE.sub(r"[\1]", text)
    text = _FIRM_RE.sub("the assessment team", text)
    return text


# ---------------------------------------------------------------------------
# Section splitting
# ---------------------------------------------------------------------------

# Anchored to line start (``^`` under MULTILINE, only horizontal whitespace before the ``#``) so a
# literal "# Impact" appearing mid-sentence in body prose can't mis-split a record.
_SECTION_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("description", re.compile(r"^[ \t]*#\s*Description\b", re.IGNORECASE | re.MULTILINE)),
    ("impact", re.compile(r"^[ \t]*#\s*Impact\b", re.IGNORECASE | re.MULTILINE)),
    ("replication", re.compile(r"^[ \t]*#\s*Replication\s+Steps\b", re.IGNORECASE | re.MULTILINE)),
)


def split_description_sections(description: str | None) -> dict[str, str]:
    """Split a FACTION ``Description`` blob on its section markers into raw per-section HTML.

    Robust to markers being missing (record predates the convention), reordered, or the whole string
    having no markers at all (treated as the description section verbatim). Any text preceding the
    first recognized marker is folded into the ``description`` section rather than dropped.
    """
    text = description or ""
    hits: list[tuple[int, int, str]] = []
    for key, pat in _SECTION_PATTERNS:
        m = pat.search(text)
        if m:
            hits.append((m.start(), m.end(), key))
    hits.sort(key=lambda h: h[0])

    sections = {"description": "", "impact": "", "replication": ""}
    if not hits:
        sections["description"] = text.strip()
        return sections

    leading = text[: hits[0][0]].strip()
    for i, (_start, end, key) in enumerate(hits):
        stop = hits[i + 1][0] if i + 1 < len(hits) else len(text)
        sections[key] = text[end:stop].strip()
    if leading:
        sections["description"] = (leading + "\n\n" + sections["description"]).strip()
    return sections


# ---------------------------------------------------------------------------
# Tolerant HTML -> tree
# ---------------------------------------------------------------------------

_VOID_TAGS = {"br", "img", "hr"}


@dataclass
class _El:
    tag: str
    attrs: dict[str, str] = field(default_factory=dict)
    children: list[_El | str] = field(default_factory=list)


class _TreeBuilder(HTMLParser):
    """Tolerant HTML -> tree builder for the small tag subset FACTION emits.

    Never raises on mismatched/unclosed tags: an end tag pops the stack up to the nearest matching
    open ancestor; if none is open, the stray end tag is ignored.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _El("#root")
        self._stack: list[_El] = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        el = _El(tag.lower(), {k: (v or "") for k, v in attrs})
        self._stack[-1].children.append(el)
        if tag.lower() not in _VOID_TAGS:
            self._stack.append(el)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._stack[-1].children.append(_El(tag.lower(), {k: (v or "") for k, v in attrs}))

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        for i in range(len(self._stack) - 1, 0, -1):
            if self._stack[i].tag == tag:
                del self._stack[i:]
                return
        # No matching open tag anywhere on the stack: ignore the stray close rather than corrupting
        # the tree or raising.

    def handle_data(self, data: str) -> None:
        if data:
            self._stack[-1].children.append(data)


def _parse_tree(html_str: str) -> _El:
    builder = _TreeBuilder()
    try:
        builder.feed(html_str or "")
        builder.close()
    except Exception:
        # Never let malformed source markup break the seed import: fall back to the bare text.
        return _El("#root", children=[re.sub(r"<[^>]+>", " ", html_str or "")])
    return builder.root


# ---------------------------------------------------------------------------
# Tree -> ProseMirror nodes
# ---------------------------------------------------------------------------

_MARK_TAGS = {
    "strong": "bold",
    "b": "bold",
    "em": "italic",
    "i": "italic",
    "u": "underline",
    "s": "strike",
    "strike": "strike",
    "code": "code",
}
_INLINE_TAGS = set(_MARK_TAGS) | {"span", "a", "br"}
_WS_RE = re.compile(r"[ \t\r\n]+")


def _add_mark(marks: list[dict[str, Any]], mark: dict[str, Any]) -> list[dict[str, Any]]:
    if any(m.get("type") == mark["type"] for m in marks):
        return marks
    return [*marks, mark]


def _text_node(text: str, marks: list[dict[str, Any]] | None = None) -> dict[str, Any] | None:
    if not text:
        return None
    node: dict[str, Any] = {"type": schema.TEXT, "text": text}
    if marks:
        node["marks"] = marks
    return node


def _merge_adjacent_text(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Coalesce consecutive text nodes with identical marks (tidy, not load-bearing)."""
    out: list[dict[str, Any]] = []
    for n in nodes:
        if (
            out
            and n.get("type") == schema.TEXT
            and out[-1].get("type") == schema.TEXT
            and out[-1].get("marks") == n.get("marks")
        ):
            out[-1] = {**out[-1], "text": out[-1]["text"] + n.get("text", "")}
        else:
            out.append(n)
    return out


def _convert_inline(node: _El | str, marks: list[dict[str, Any]], in_code: bool) -> list[dict[str, Any]]:
    if isinstance(node, str):
        text = node if in_code else normalize_tokens(node)
        if not in_code:
            text = _WS_RE.sub(" ", text)
        n = _text_node(text, marks)
        return [n] if n else []

    tag = node.tag
    if tag == "br":
        return [{"type": schema.HARD_BREAK}]
    if tag == "a":
        href = node.attrs.get("href") or "#"
        new_marks = _add_mark(marks, {"type": "link", "attrs": {"href": href}})
        return _convert_children_inline(node, new_marks, in_code)
    if tag in _MARK_TAGS:
        new_marks = _add_mark(marks, {"type": _MARK_TAGS[tag]})
        return _convert_children_inline(node, new_marks, in_code or tag == "code")
    if tag == "span":
        classes = set((node.attrs.get("class") or "").split())
        new_marks = marks
        if "bold" in classes:
            new_marks = _add_mark(new_marks, {"type": "bold"})
        if "italic" in classes:
            new_marks = _add_mark(new_marks, {"type": "italic"})
        return _convert_children_inline(node, new_marks, in_code)
    # Unknown inline tag (or a block tag stumbled into mid-run, e.g. malformed source): degrade to
    # its text, dropping the wrapper but never dropping the content.
    return _convert_children_inline(node, marks, in_code)


def _convert_children_inline(node: _El, marks: list[dict[str, Any]], in_code: bool) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for child in node.children:
        out.extend(_convert_inline(child, marks, in_code))
    return out


def _extract_code_text(node: _El | str) -> str:
    """Flatten a <pre> subtree to plain text for a codeBlock (no marks; <br> -> newline)."""
    if isinstance(node, str):
        return node
    if node.tag == "br":
        return "\n"
    return "".join(_extract_code_text(c) for c in node.children)


def _empty_paragraph() -> dict[str, Any]:
    return {"type": schema.PARAGRAPH, "content": []}


def _is_blank_run(content: list[dict[str, Any]]) -> bool:
    """A would-be paragraph that carries only whitespace text and/or hard breaks -> drop it (so a
    ``\\n\\n`` gap between two block elements never becomes a spurious empty paragraph)."""
    return all(
        c.get("type") == schema.HARD_BREAK
        or (c.get("type") == schema.TEXT and not c.get("text", "").strip())
        for c in content
    )


def _blocks_from_children(children: list[_El | str]) -> list[dict[str, Any]]:
    """Convert a mixed run of block-level and inline-level children into block nodes.

    Consecutive inline-level children (text, ``<strong>``/``<em>``/``<a>``/``<span>``/``<br>``/...)
    are coalesced into a SINGLE paragraph rather than one paragraph each, so bare top-level inline
    content like ``Some text <strong>bold</strong> more`` (no wrapping ``<p>``) round-trips as one
    paragraph, not three fragments.
    """
    blocks: list[dict[str, Any]] = []
    inline_buf: list[_El | str] = []

    def flush() -> None:
        if not inline_buf:
            return
        buf_node = _El("#buf", children=list(inline_buf))
        content = _merge_adjacent_text(_convert_children_inline(buf_node, [], False))
        inline_buf.clear()
        if content and not _is_blank_run(content):
            blocks.append({"type": schema.PARAGRAPH, "content": content})

    for child in children:
        if isinstance(child, str) or child.tag in _INLINE_TAGS:
            inline_buf.append(child)
        else:
            flush()
            blocks.extend(_convert_block(child))
    flush()
    return blocks


def _convert_list_item(li: _El) -> dict[str, Any] | None:
    blocks = _blocks_from_children(li.children)
    if not blocks:
        return None  # an empty <li> carries no content: drop it rather than emit an empty bullet
    # TipTap's listItem content model is `paragraph block*`: the first child MUST be a paragraph, or an
    # editor/Yjs round-trip can drop the leading block. A <li> that starts with a nested list (its only
    # child a <ul>/<ol>) would otherwise produce an invalid listItem -> prepend an empty paragraph.
    if blocks[0].get("type") != schema.PARAGRAPH:
        blocks.insert(0, _empty_paragraph())
    return {"type": schema.LIST_ITEM, "content": blocks}


def _convert_block(node: _El | str) -> list[dict[str, Any]]:
    """Convert one node, seen at block level, into zero or more ProseMirror block nodes."""
    if isinstance(node, str):
        text = normalize_tokens(_WS_RE.sub(" ", node)).strip()
        if not text:
            return []
        return [{"type": schema.PARAGRAPH, "content": [{"type": schema.TEXT, "text": text}]}]

    tag = node.tag

    if tag in {"p", "div"}:
        content = _merge_adjacent_text(_convert_children_inline(node, [], False))
        # Drop paragraphs that carry no real content (e.g. a stray `<p><br /></p>` spacer or an
        # empty `<p></p>`) so they don't clutter the report with blank lines.
        if not content or all(c.get("type") == schema.HARD_BREAK for c in content):
            return []
        return [{"type": schema.PARAGRAPH, "content": content}]

    if tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
        level = int(tag[1])
        content = _merge_adjacent_text(_convert_children_inline(node, [], False))
        if not content:
            return []
        return [{"type": schema.HEADING, "attrs": {"level": level}, "content": content}]

    if tag in {"ul", "ol"}:
        items = [
            _convert_list_item(c) for c in node.children if isinstance(c, _El) and c.tag == "li"
        ]
        items = [i for i in items if i is not None]
        if not items:
            return []
        list_type = schema.BULLET_LIST if tag == "ul" else schema.ORDERED_LIST
        return [{"type": list_type, "content": items}]

    if tag == "li":
        # A stray <li> outside any ul/ol (malformed source): treat it as a bullet list of one.
        item = _convert_list_item(node)
        return [{"type": schema.BULLET_LIST, "content": [item]}] if item else []

    if tag == "pre":
        code_text = _extract_code_text(node)
        if not code_text.strip():
            return []
        return [{"type": schema.CODE_BLOCK, "content": [{"type": schema.TEXT, "text": code_text}]}]

    if tag == "blockquote":
        inner = _convert_children_block(node)
        if not inner:
            return []
        return [{"type": schema.BLOCKQUOTE, "content": inner}]

    if tag == "br":
        return []  # a standalone hard break at block level carries no content

    if tag in _INLINE_TAGS:
        # Bare inline markup at the top of a fragment (no wrapping <p>): still real content, just
        # missing its paragraph wrapper in the source. Wrap it in one.
        content = _merge_adjacent_text(_convert_inline(node, [], False))
        return [{"type": schema.PARAGRAPH, "content": content}] if content else []

    # Truly unknown block-level wrapper (e.g. a tag this dataset doesn't use): promote its children
    # into the surrounding block context rather than silently dropping the content.
    return _convert_children_block(node)


def _convert_children_block(node: _El) -> list[dict[str, Any]]:
    return _blocks_from_children(node.children)


def html_to_doc(html_str: str | None) -> dict[str, Any]:
    """Parse a FACTION HTML fragment into a ProseMirror ``doc`` (``schema`` module). Never raises;
    empty/whitespace-only input yields ``schema.empty_doc()``."""
    if not html_str or not html_str.strip():
        return schema.empty_doc()
    try:
        tree = _parse_tree(html_str)
        content = _convert_children_block(tree)
    except Exception:
        # Absolute last resort: strip tags and keep the text rather than losing the record or crashing
        # the whole seed import over one malformed HTML fragment.
        text = normalize_tokens(re.sub(r"<[^>]+>", " ", html_str)).strip()
        para = {"type": schema.PARAGRAPH, "content": [{"type": schema.TEXT, "text": text}]}
        content = [para] if text else []
    return {"type": schema.DOC, "content": content}


def bold_lead_paragraph(text: str) -> dict[str, Any]:
    """A single bold-text paragraph, used to fold the "# Impact" section under a labeled lead-in
    inside the ``description`` block (Fraction has no separate impact block)."""
    return {
        "type": schema.PARAGRAPH,
        "content": [{"type": schema.TEXT, "text": text, "marks": [{"type": "bold"}]}],
    }


# ---------------------------------------------------------------------------
# FACTION record -> Fraction content blocks
# ---------------------------------------------------------------------------


def build_template_blocks(description: str | None, recommendation: str | None) -> dict[str, dict[str, Any]]:
    """Map one FACTION record's ``Description``/``Recommendation`` fields onto Fraction's
    ``description``/``details``/``remediation`` content blocks (``schema.DEFAULT_BLOCKS``).

    - ``description`` <- the "# Description" section, with the "# Impact" section (if present and
      non-empty) folded in underneath a bold "Impact" lead paragraph, since Fraction has no separate
      impact block.
    - ``details`` <- the "# Replication Steps" section.
    - ``remediation`` <- the ``Recommendation`` field, parsed the same way.

    A missing/empty section yields ``schema.empty_doc()`` for that block, never a stray header.
    """
    sections = split_description_sections(description)
    description_doc = html_to_doc(sections["description"])
    if sections["impact"]:
        impact_doc = html_to_doc(sections["impact"])
        impact_content = impact_doc["content"]
        if impact_content:
            description_doc = {
                "type": schema.DOC,
                "content": [
                    *description_doc["content"],
                    bold_lead_paragraph("Impact"),
                    *impact_content,
                ],
            }
    return {
        "description": description_doc,
        "details": html_to_doc(sections["replication"]),
        "remediation": html_to_doc(recommendation),
    }
