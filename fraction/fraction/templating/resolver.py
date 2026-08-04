"""Build a variable context and resolve ``{{ }}`` placeholders with a sandboxed Jinja environment.

Built-in variables are derived from the engagement + finding; custom variables come from
``VariableValue`` rows (WS6 wires those in fully). Resolution uses ``jinja2.sandbox`` so untrusted
template text can never reach attribute access or arbitrary evaluation.
"""

from __future__ import annotations

import copy
from collections.abc import Callable
from typing import Any

from jinja2 import TemplateError, Undefined
from jinja2.sandbox import SandboxedEnvironment

from fraction.content import schema

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
    try:
        return _ENV.from_string(text).render(**ctx)
    except TemplateError:
        return text


def make_var_resolver(ctx: dict[str, Any]) -> Callable[[str], str]:
    """A ``resolve_var(key) -> str`` callback for the HTML/docx walkers' ``variable`` nodes."""

    def _resolve(key: str) -> str:
        value = ctx.get(key)
        return "" if value is None else str(value)

    return _resolve


def resolve_doc(doc: dict | None, ctx: dict[str, Any]) -> dict | None:
    """Return a copy of a ProseMirror doc with ``{{ }}`` in text nodes and ``variable`` nodes resolved."""
    if not doc:
        return doc
    out = copy.deepcopy(doc)

    def _walk(node: dict) -> None:
        t = node.get("type")
        if t == schema.TEXT and node.get("text"):
            node["text"] = resolve_text(node["text"], ctx)
        elif t == schema.VARIABLE:
            key = node.get("attrs", {}).get("key", "")
            node["type"] = schema.TEXT
            node["text"] = str(ctx.get(key, "{{" + key + "}}"))
            node.pop("attrs", None)
        for child in node.get("content", []) or []:
            _walk(child)

    _walk(out)
    return out
