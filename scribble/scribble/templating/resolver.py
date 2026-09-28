"""Build a variable context and resolve ``{{ }}`` placeholders with a sandboxed Jinja environment.

Built-in variables are derived from the engagement + finding; custom variables come from
``VariableValue`` rows (WS6 wires those in fully). Resolution uses ``jinja2.sandbox`` so untrusted
template text can never reach attribute access or arbitrary evaluation.
"""

from __future__ import annotations

import copy
import re
from collections.abc import Callable
from typing import Any

from jinja2 import TemplateError, Undefined
from jinja2.sandbox import SandboxedEnvironment

from scribble.content import schema

BUILTIN_KEYS = (
    "COMPANY_NAME",
    "ENGAGEMENT_NAME",
    "TARGET_HOST",
    "TARGET_PORT",
    "TARGET_URL",
    "ASSESSOR",
    "TODAY",
    "START_DATE",
    "END_DATE",
    "SEVERITY",
)


class _KeepUndefined(Undefined):
    """Leave an unknown ``{{KEY}}`` untouched rather than blanking it (so previews flag gaps)."""

    def __str__(self) -> str:  # pragma: no cover - trivial
        return "{{" + (self._undefined_name or "") + "}}"


_ENV = SandboxedEnvironment(
    variable_start_string="{{",
    variable_end_string="}}",
    undefined=_KeepUndefined,
    autoescape=False,
)


def _fmt_date(value) -> str:
    return value.isoformat() if value else ""


def is_unpopulated(value: Any) -> bool:
    """True when a variable has no usable value — ``None`` or a string that is empty/whitespace-only.

    This is the "unpopulated" test LOT-63 hinges on: a known variable that was never filled in (e.g. a
    blank ``COMPANY_NAME``) must be surfaced exactly like an unknown tag, not shipped as a silent blank.
    """
    return value is None or (isinstance(value, str) and value.strip() == "")


def build_context(engagement, finding=None, *, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """Assemble the {{VARIABLE}} context. ``extra`` overlays custom variable values (from WS6)."""
    import datetime as _dt

    company = getattr(engagement, "company_name", None)
    if not company:
        client = getattr(engagement, "client", None)
        company = getattr(client, "name", "") if client else ""

    ctx: dict[str, Any] = {
        "COMPANY_NAME": company or "",
        "ENGAGEMENT_NAME": getattr(engagement, "name", "") or "",
        "TODAY": _dt.date.today().isoformat(),
        "START_DATE": _fmt_date(getattr(engagement, "start_date", None)),
        "END_DATE": _fmt_date(getattr(engagement, "end_date", None)),
        "TARGET_HOST": "",
        "TARGET_PORT": "",
        "TARGET_URL": "",
        "ASSESSOR": getattr(engagement, "created_by", "") or "",
        "SEVERITY": "",
    }
    if finding is not None:
        ctx["TARGET_HOST"] = getattr(finding, "target_host", "") or ""
        ctx["TARGET_PORT"] = getattr(finding, "target_port", "") or ""
        ctx["TARGET_URL"] = getattr(finding, "target_url", "") or ""
        sev = getattr(finding, "severity", None)
        ctx["SEVERITY"] = getattr(sev, "value", "") if sev is not None else ""
    if extra:
        ctx.update(extra)
    return ctx


# Audit W-12: report text blocks are small. SSTI->RCE is already contained by the SandboxedEnvironment
# (``_ENV`` above restricts attribute access), so the residual on this attacker-supplyable /preview text is
# compute/memory DoS (e.g. ``{{ "x" * 10**9 }}`` or huge templates). Refuse to render a pathologically
# large template as a cheap amplification bound (a render timeout at the worker level would bound the
# tiny-input-huge-output case — tracked as a follow-up).
_MAX_TEMPLATE_LEN = 100_000


def resolve_text(text: str, ctx: dict[str, Any]) -> str:
    """Render a single string's ``{{ }}`` against the context.

    Returns the text unchanged if it is not a valid template — imported seed content can carry foreign
    tokens (e.g. ``{{.pass_pol}}``) that are not valid Jinja; those must survive verbatim, not crash — or
    if it exceeds ``_MAX_TEMPLATE_LEN`` (W-12 DoS bound).
    """
    if not text or "{{" not in text:
        return text
    if len(text) > _MAX_TEMPLATE_LEN:
        return text  # too large to safely render — pass through unrendered (W-12)
    # LOT-63: an unpopulated known variable is rendered as *undefined*, not as "", so the literal
    # ``{{KEY}}`` survives (via ``_KeepUndefined``) for the doc walker to highlight. Dropping the empty
    # entries here means a blank ``COMPANY_NAME`` is loud in the report instead of a silent gap.
    render_ctx = {k: v for k, v in ctx.items() if not is_unpopulated(v)}
    try:
        return _ENV.from_string(text).render(**render_ctx)
    except TemplateError:
        return text


def make_var_resolver(ctx: dict[str, Any]) -> Callable[[str], str]:
    """A ``resolve_var(key) -> str`` callback for the HTML/docx walkers' ``variable`` nodes."""

    def _resolve(key: str) -> str:
        value = ctx.get(key)
        return "" if value is None else str(value)

    return _resolve


# A rendered ``{{...}}`` that stayed unresolved: either an unknown tag left literal by ``_KeepUndefined``,
# or a foreign token (``{{.pass_pol}}``) the resolver passed through verbatim. Both get highlighted.
# The body is ``[^{}]*`` (not ``.*?``) on purpose: a real token never nests braces, and the negated
# class matches newlines too, so this needs no DOTALL. It also stays linear on adversarial input like
# ``{{{{{{...`` — ``.*?`` there is O(n^2) backtracking (a polynomial-ReDoS flag), because every ``{{``
# start rescans to end hunting a ``}}``; ``[^{}]*`` fails at the next brace, so each start is O(1).
_UNRESOLVED_TOKEN_RE = re.compile(r"\{\{[^{}]*\}\}")


def _with_unresolved_mark(marks: list[dict] | None) -> list[dict]:
    """Return ``marks`` plus the ``unresolvedVar`` mark (idempotent — never doubles it)."""
    existing = list(marks or [])
    if any(m.get("type") == schema.MARK_UNRESOLVED for m in existing):
        return existing
    return existing + [{"type": schema.MARK_UNRESOLVED}]


def _split_resolved_text(node: dict, ctx: dict[str, Any]) -> list[dict]:
    """Resolve one text node's ``{{ }}`` and split it into text nodes, marking any token that stayed
    unresolved so the renderers can highlight it (LOT-63). Preserves the node's existing marks."""
    rendered = resolve_text(node.get("text", ""), ctx)
    base_marks = node.get("marks") or []

    segments: list[tuple[str, bool]] = []
    last = 0
    for m in _UNRESOLVED_TOKEN_RE.finditer(rendered):
        if m.start() > last:
            segments.append((rendered[last:m.start()], False))
        segments.append((m.group(0), True))
        last = m.end()
    if last < len(rendered):
        segments.append((rendered[last:], False))

    if len(segments) <= 1 and not (segments and segments[0][1]):
        # Nothing unresolved: keep it a single text node (identical shape to the old behavior).
        node["text"] = rendered
        return [node]

    out: list[dict] = []
    for text, unresolved in segments:
        if not text:
            continue
        piece: dict = {"type": schema.TEXT, "text": text}
        marks = _with_unresolved_mark(base_marks) if unresolved else list(base_marks)
        if marks:
            piece["marks"] = marks
        out.append(piece)
    return out or [node]


def _resolve_variable_node(node: dict, ctx: dict[str, Any]) -> dict:
    """Turn a ``variable`` node into a text node: its value if populated, else a highlighted ``{{KEY}}``."""
    key = node.get("attrs", {}).get("key", "")
    value = ctx.get(key)
    base_marks = node.get("marks") or []
    if key in ctx and not is_unpopulated(value):
        out: dict = {"type": schema.TEXT, "text": str(value)}
        if base_marks:
            out["marks"] = list(base_marks)
        return out
    return {
        "type": schema.TEXT,
        "text": "{{" + key + "}}",
        "marks": _with_unresolved_mark(base_marks),
    }


def resolve_doc(doc: dict | None, ctx: dict[str, Any]) -> dict | None:
    """Return a copy of a ProseMirror doc with ``{{ }}`` in text nodes and ``variable`` nodes resolved.

    Unresolved tags (unknown, or known-but-unpopulated) are stamped with the ``unresolvedVar`` mark so the
    HTML/DOCX renderers highlight them in yellow (LOT-63) rather than shipping a literal ``{{KEY}}`` or a
    silent blank.
    """
    if not doc:
        return doc
    out = copy.deepcopy(doc)

    def _walk(node: dict) -> None:
        children = node.get("content")
        if not children:
            return
        new_children: list[dict] = []
        for child in children:
            ctype = child.get("type")
            if ctype == schema.TEXT and child.get("text") and "{{" in child["text"]:
                new_children.extend(_split_resolved_text(child, ctx))
            elif ctype == schema.VARIABLE:
                new_children.append(_resolve_variable_node(child, ctx))
            else:
                _walk(child)
                new_children.append(child)
        node["content"] = new_children

    _walk(out)
    return out
