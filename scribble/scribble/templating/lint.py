"""Preview/lint helpers: find ``{{KEY}}`` references and flag the ones that won't resolve to anything.

``lint_text``/``lint_doc`` are DB-free by default (checked only against ``BUILTIN_KEYS``); pass
``known_keys=known_variable_keys(session)`` (see ``scribble.templating.context``) to also recognize
defined custom ``TemplateVariable``s. ``resolve_finding`` is the single-finding preview convenience:
build the full context, resolve every content block, render to sanitized HTML -- the same three steps
WS7's report pipeline runs per finding (see ``reporting/context.py:_finding_ctx``), usable standalone.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from sqlalchemy import func, select

from scribble.content import schema
from scribble.content.render_html import render_block
from scribble.models import BoardFinding
from scribble.templating.context import build_full_context
from scribble.templating.resolver import BUILTIN_KEYS, build_context, make_var_resolver, resolve_doc

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


def finding_overlay_variables(session, finding) -> dict[str, str]:
    """The ``{{TOKEN}}`` overlay for rendering a finding's prose: its promoted ``variables`` (filled from
    the host's neutral facts at promote -- CONTRACT.md §5.4), with an AGGREGATION PARENT's
    ``{{AFFECTED}}``/``{{TARGET_URL}}`` collapsed to a count-reference instead of a per-host wall (Blade
    UX). The ONE home both the deliverable render AND the live preview use, so a preview resolves
    variables identically to the report (ext#: preview was building context with NO overlay and drifted)."""
    variables = dict(getattr(finding, "variables", None) or {})
    fid = getattr(finding, "id", None)
    if session is not None and fid is not None:
        child_count = session.scalar(
            select(func.count()).select_from(BoardFinding).where(BoardFinding.parent_id == fid)
        ) or 0
        if child_count:
            ref = f"{child_count} host{'' if child_count == 1 else 's'} (see Affected Assets below)"
            variables["AFFECTED"] = ref
            variables["TARGET_URL"] = ref
    return variables


def resolve_finding(session, finding, *, artifact_url=None, suppressed=()) -> dict[str, str]:
    """Resolve + render ``finding``'s content blocks to sanitized HTML -- the ONE per-finding slice of the
    report pipeline, used by the deliverable renderer (``reporting.context._finding_ctx``) AND the live
    preview (``templating_api``) so the two never drift.

    Applies the finding's variable overlay (:func:`finding_overlay_variables`), renders the
    ``reproduction`` block VERBATIM (no variable substitution -- it carries scanner PoC payloads like
    ``{{7*7}}`` that must never be evaluated, or the block stops reproducing the bug), and skips any
    ``suppressed`` section (the report passes its suppressed set; a preview passes none, showing all)."""
    variables = finding_overlay_variables(session, finding)
    ctx = (
        build_full_context(session, finding.engagement, finding, extra=variables)
        if session is not None
        else build_context(finding.engagement, finding, extra=variables)
    )
    resolve_var = make_var_resolver(ctx)
    out: dict[str, str] = {}
    for block, doc in (finding.content_json or {}).items():
        if block in suppressed:
            continue
        if block == "reproduction":
            out[block] = render_block(doc, artifact_url=artifact_url)  # verbatim -- no var resolution
            continue
        out[block] = render_block(resolve_doc(doc, ctx), resolve_var=resolve_var, artifact_url=artifact_url)
    return out
