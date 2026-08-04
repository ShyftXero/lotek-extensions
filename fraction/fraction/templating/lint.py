"""Preview/lint helpers: find ``{{KEY}}`` references and flag the ones that won't resolve to anything.

``lint_text``/``lint_doc`` are DB-free by default (checked only against ``BUILTIN_KEYS``); pass
``known_keys=known_variable_keys(session)`` (see ``fraction.templating.context``) to also recognize
defined custom ``TemplateVariable``s. ``resolve_finding`` is the single-finding preview convenience:
build the full context, resolve every content block, render to sanitized HTML -- the same three steps
WS7's report pipeline runs per finding (see ``reporting/context.py:_finding_ctx``), usable standalone.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from fraction.content import schema
from fraction.content.render_html import render_block
from fraction.templating.context import build_full_context
from fraction.templating.resolver import BUILTIN_KEYS, make_var_resolver, resolve_doc

# Plain regex scan, not a Jinja parse: a foreign/invalid token that ``resolve_text`` leaves verbatim
# (e.g. ``{{.pass_pol}}``, inherited from FACTION's ``${}`` seed content) must still be picked up here so
# it can be flagged, even though Jinja itself would refuse to compile it.
_TOKEN_RE = re.compile(r"\{\{\s*(.*?)\s*\}\}")


def _extract_tokens(text: str | None) -> list[str]:
    if not text or "{{" not in text:
        return []
    return [tok.strip() for tok in _TOKEN_RE.findall(text) if tok.strip()]


def lint_text(text: str | None, known_keys: Iterable[str] = ()) -> list[str]:
    """Sorted, deduped list of ``{{...}}`` tokens referenced in ``text`` that are neither a
    ``BUILTIN_KEYS`` name nor in ``known_keys``."""
    known = set(BUILTIN_KEYS) | set(known_keys)
    return sorted({tok for tok in _extract_tokens(text) if tok not in known})


def lint_doc(doc: dict | None, known_keys: Iterable[str] = ()) -> list[str]:
    """Same as :func:`lint_text`, walking a ProseMirror doc's ``text`` node bodies and ``variable`` node
    ``attrs.key`` references."""
    known = set(BUILTIN_KEYS) | set(known_keys)
    unknown: set[str] = set()
    for node in schema.iter_nodes(doc):
        ntype = node.get("type")
        if ntype == schema.TEXT:
            unknown.update(tok for tok in _extract_tokens(node.get("text")) if tok not in known)
        elif ntype == schema.VARIABLE:
            key = node.get("attrs", {}).get("key", "")
            if key and key not in known:
                unknown.add(key)
    return sorted(unknown)


def resolve_finding(session, finding, *, artifact_url=None) -> dict[str, str]:
    """Resolve + render every content block of ``finding`` to sanitized HTML.

    Combines :func:`fraction.templating.context.build_full_context` (built-ins + engagement/finding
    custom-variable overlay) with ``resolver.resolve_doc`` and ``content.render_html.render_block`` --
    the per-finding slice of WS7's report pipeline, usable standalone for a live preview."""
    ctx = build_full_context(session, finding.engagement, finding)
    resolve_var = make_var_resolver(ctx)
    return {
        block: render_block(resolve_doc(doc, ctx), resolve_var=resolve_var, artifact_url=artifact_url)
        for block, doc in (finding.content_json or {}).items()
    }
