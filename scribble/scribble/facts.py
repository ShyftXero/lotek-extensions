"""scribble/facts.py — the ONE shape rule table interpreting the DB-declared fact -> report-variable
mapping (``TemplateVariable.from_facts`` / ``.target_column``, CONTRACT-FACTS.md §4).

WHY: the host (lotek core) only ever hands over a neutral, OPEN ``facts`` dict per finding
(``app.host_contract.FindingDTO.facts`` — see that module's own "DECLARED facts" banner). What a
``{{TOKEN}}`` in a report DOES with those facts is this repo's own vocabulary, and it too is DATA: a row
in ``scribble_variables`` (``TemplateVariable.from_facts``), not Python keyed on a tool's name. This
module is the interpreter for that data — it is generic BY VALUE SHAPE (``scalar`` / ``list`` / ``count``
/ ``host`` / ``severity`` / ``parent_domain``), never by tool or by ``dto.source``. Adding a tool that
emits a new fact, or a report author wanting a new token, is a data edit on either side of this file —
never a change to it.

BEST EFFORT BY CONTRACT, same posture as the host's own declared-facts engine: an absent fact/field tries
the next candidate rule; a rule that cannot be rendered (bad shape, failed coercion) is skipped, never
raised; a malformed ``from_facts`` (not a JSON list) yields no rules at all, not an exception. Nothing in
this module can fail a promote.

PURE: no session, no I/O, no clock, no randomness — safe to unit-test exhaustively (see
``tests/test_facts_shapes.py`` in this repo) and safe to call from ``scribble/promote.py`` per finding.
"""

from __future__ import annotations

import re
from typing import Any

from scribble.enums import Severity, severity_rank

# Bounds mirror the host's own declared-facts engine (app.host_contract) — a declaration is data, but
# still must not make derivation long or expensive.
_MAX_RULES = 32
_LIST_CAP = 40

# Allowlisted FindingDTO attributes a rule may reference via ``{"field": "..."}`` (as opposed to
# ``{"fact": "..."}``, which reads the OPEN ``dto.facts`` dict). This is a fixed, generic contract
# boundary — plain dataclass attribute names, never a tool or source literal — so a declaration can
# reach "the DTO's own severity" or "the DTO's own resolved host" without this file growing per-tool
# knowledge.
_ALLOWED_FIELDS = frozenset(
    {"target_host", "severity", "source", "category", "title", "cve", "cvss_score", "confidence", "status"}
)

# Generic host-shape recognizer / URL-scheme prefix. Reimplemented here (not imported) because scribble
# never imports lotek core — this is a property of the VALUE, not lotek-specific code.
_HOSTISH_RE = re.compile(
    r"^(?:\d{1,3}(?:\.\d{1,3}){3}|[0-9a-f:]+:[0-9a-f:]+|[a-z0-9](?:[a-z0-9._-]*[a-z0-9])?)$", re.I
)
_URL_SCHEME_RE = re.compile(r"^[a-z][a-z0-9+.\-]*://", re.I)
_IPV4_RE = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")


def _is_empty(value: Any) -> bool:
    """A resolved value that counts as ABSENT — mirrors the host's own ``_is_empty_fact``: a tool/field
    writing ``""``/``[]``/``{}`` must not beat a later candidate rule."""
    return value is None or value == "" or value == [] or value == {}


def _raw_value(dto: Any, rule: dict) -> Any:
    """The value ONE rule points at: ``facts[fact]`` (the open dict) or an allowlisted DTO attribute.
    Never raises — an unrecognized ``field``, or a rule naming neither, simply resolves to ``None``
    (absent), same best-effort posture as everything else here."""
    if "fact" in rule:
        facts = getattr(dto, "facts", None)
        return facts.get(rule["fact"]) if isinstance(facts, dict) else None
    if "field" in rule:
        field = rule.get("field")
        return getattr(dto, field, None) if field in _ALLOWED_FIELDS else None
    return None


def _bare_host(value: Any) -> str | None:
    """Generic host-shape extraction: strip a scheme, userinfo, path/query/fragment, an IPv6 bracket
    pair, and a trailing ``:port``, then validate the remainder. A property of the VALUE's shape, not of
    any tool — this is what the ``"host"`` shape resolves to."""
    text = str(value or "").strip()
    if not text:
        return None
    text = _URL_SCHEME_RE.sub("", text, count=1)
    for sep in ("/", "?", "#"):
        text = text.split(sep, 1)[0]
    if "@" in text:
        text = text.rsplit("@", 1)[-1]
    if text.startswith("["):
        text = text[1:].split("]", 1)[0]
    elif text.count(":") == 1:  # host:port -> host (a bare IPv6 has more than one colon)
        text = text.split(":", 1)[0]
    return text if text and _HOSTISH_RE.match(text) else None


def _parent_domain(value: Any) -> str:
    """The parent DNS domain of a host-shaped value (``dc01.cheddarsale.local`` -> ``cheddarsale.local``);
    ``""`` for an IP address (v4 or v6) or a name of 2 labels or fewer."""
    host = _bare_host(value)
    if not host:
        return ""
    if _IPV4_RE.match(host) or ":" in host:
        return ""
    labels = host.split(".")
    if len(labels) <= 2:
        return ""
    return ".".join(labels[-2:])


def _as_list(value: Any) -> list[str]:
    """``"list"`` shape input coercion: a list stays a list, a comma-separated string is split+stripped,
    any other scalar becomes a 1-item list. Mirrors the host's own ``{"type": "list"}`` coercion (same
    value-shape rule, independently implemented — no tool knowledge either place)."""
    if isinstance(value, list):
        items: list[Any] = value
    elif isinstance(value, str) and "," in value:
        items = [p.strip() for p in value.split(",") if p.strip()]
    else:
        items = [value]
    return [str(x) for x in items if x is not None and str(x).strip() != ""]


def _join_capped(items: list[str], cap: int = _LIST_CAP) -> str | None:
    uniq = sorted(set(items))
    if not uniq:
        return None
    if len(uniq) > cap:
        shown = uniq[:cap]
        return f"{', '.join(shown)} …and {len(uniq) - cap} more"
    return ", ".join(uniq)


def _as_count(value: Any) -> int | None:
    """``"count"`` shape: a list's length, or an int-coercible scalar (int itself, or a numeric string/
    float). ``None`` when neither — the fact/field is then treated as absent, never a crash and never a
    silent ``0``."""
    if isinstance(value, list):
        return len(value)
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        pass
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _severity_key(text: str) -> int:
    """Rank a raw severity string worst-first (0 = most severe), via ``scribble.enums.severity_rank``.
    An unrecognized value sorts last (least severe) rather than raising or crashing a promote."""
    try:
        return severity_rank(Severity(text.strip().lower()))
    except ValueError:
        return 999


def _apply_template(template: Any, value: str) -> str:
    """Optional ``"{value}"`` wrapper. Never raises — a template with an unexpected placeholder simply
    yields the unwrapped value (best effort: a bad declaration degrades a token, it never breaks one)."""
    try:
        return str(template).format(value=value)
    except (KeyError, IndexError, ValueError, AttributeError):
        return value


def _render_single(raw: Any, shape: str) -> str | None:
    """One rule's base text for ONE finding, before any ``template`` wrap. ``None`` means this rule did
    not resolve — the caller tries the next candidate rule."""
    if shape == "list":
        return _join_capped(_as_list(raw))
    if shape == "count":
        n = _as_count(raw)
        return None if n is None else str(n)
    if shape == "host":
        return _bare_host(raw)
    if shape == "severity":
        text = str(raw).strip()
        return text or None
    if shape == "parent_domain":
        dom = _parent_domain(raw)
        return dom or None
    # "scalar", and any unrecognized shape string -- degrades to scalar (best effort: an unknown `shape`
    # never raises, it just renders the value verbatim).
    text = str(raw).strip()
    return text or None


def resolve_variables(dto: Any, declarations: Any) -> dict[str, str]:
    """``{VARIABLE: value}`` for ONE finding, from the host's neutral ``dto.facts``/attributes + the
    DB-declared ``from_facts`` rules. Pure.

    ``declarations`` is ``[(key, target_column, rules), ...]`` — ``rules`` an ORDERED list of
    ``{"fact"|"field": "...", "shape": "...", "template"?: "..."}`` dicts read straight off
    ``TemplateVariable.from_facts``. The first rule that resolves non-empty wins (declaration order is
    the fallback chain). Contains no tool name and no ``dto.source``/``==`` branch anywhere: the only
    inputs are the DTO and the declaration.
    """
    return {key: _resolve_one(dto, rules) for key, _target_column, rules in declarations}


def _resolve_one(dto: Any, rules: Any) -> str:
    if not isinstance(rules, list):
        return ""
    for rule in rules[:_MAX_RULES]:
        if not isinstance(rule, dict):
            continue
        raw = _raw_value(dto, rule)
        if _is_empty(raw):
            continue
        shape = str(rule.get("shape") or "scalar").strip().lower()
        text = _render_single(raw, shape)
        if text is None:
            continue
        template = rule.get("template")
        if template:
            text = _apply_template(template, text)
        return text
    return ""


def _aggregate(raws: list[Any], shape: str) -> str | None:
    """Combine N children's RAW values for one rule into the parent's text, by shape (§4.2's table,
    column 2). ``None`` means this rule had nothing usable across every child — the caller tries the next
    candidate rule. An empty string (``""``) is a DEFINITE, final answer (e.g. two children disagree on a
    ``scalar``/``parent_domain`` value) — it is not the same as "try the next rule"."""
    if shape == "list":
        items: list[str] = []
        for r in raws:
            items.extend(_as_list(r))
        return _join_capped(items)
    if shape == "count":
        total = 0
        any_ok = False
        for r in raws:
            n = _as_count(r)
            if n is not None:
                total += n
                any_ok = True
        return str(total) if any_ok else None
    if shape == "host":
        hosts = sorted({h for h in (_bare_host(r) for r in raws) if h})
        return ", ".join(hosts) if hosts else None
    if shape == "severity":
        vals = [str(r).strip() for r in raws if str(r).strip()]
        return min(vals, key=_severity_key) if vals else None
    if shape == "parent_domain":
        doms = sorted({d for d in (_parent_domain(r) for r in raws) if d})
        if not doms:
            return None
        return doms[0] if len(doms) == 1 else ""
    # "scalar" (and any unrecognized shape) -- "the single distinct non-empty child value, else ''":
    # never guess between two.
    vals = sorted({str(r).strip() for r in raws if str(r).strip()})
    if not vals:
        return None
    return vals[0] if len(vals) == 1 else ""


def synthesize_parent_variables(children: Any, declarations: Any) -> dict[str, str]:
    """``{VARIABLE: value}`` for a PARENT, combining its children's DTOs BY FACT SHAPE (§4.2's table).
    Pure and deterministic: every join is over a ``sorted(set(...))``, every count an ``int`` rendered
    with ``str()``, severity ties resolve via ``scribble.enums.severity_rank``, no time/randomness/dict-
    order dependence, no network, no model. Children keep their OWN ``variables`` (computed separately via
    ``resolve_variables`` per child) — this function only produces the PARENT's row.
    """
    return {key: _synthesize_one(children, rules) for key, _target_column, rules in declarations}


def _synthesize_one(children: Any, rules: Any) -> str:
    if not isinstance(rules, list):
        return ""
    for rule in rules[:_MAX_RULES]:
        if not isinstance(rule, dict):
            continue
        raws = [v for v in (_raw_value(c, rule) for c in children) if not _is_empty(v)]
        if not raws:
            continue
        shape = str(rule.get("shape") or "scalar").strip().lower()
        text = _aggregate(raws, shape)
        if text is None:
            continue
        template = rule.get("template")
        if template and text:
            text = _apply_template(template, text)
        return text
    return ""
