"""Idempotent seeding + FACTION vuln-template import.

WS12 parses the FACTION ``Description`` (embedded HTML + ``# Description``/``# Impact``/
``# Replication Steps`` section markers) into real ``description``/``details``/``remediation``
ProseMirror blocks instead of Sprint 0's faithful-but-wrong minimal conversion (the whole blob wrapped
as literal text via ``schema.doc_from_text``, which rendered reports with literal ``<p>``/``#`` text).
See ``fraction/seed/faction_parse.py`` for the parser and the FACTION->Fraction token normalization
(``{{client}}``/``CLIENT`` -> ``{{COMPANY_NAME}}``, ``{{.foo}}`` -> ``[foo]``).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from sqlalchemy import select

from fraction.content import render_html
from fraction.enums import Severity, VariableScope, VariableType
from fraction.models import AssessmentType, FractionVulnMap, TemplateVariable, VulnerabilityTemplate
from fraction.seed import faction_parse
from fraction.templating import BUILTIN_KEYS

# Allowlist for TemplateVariable.target_column (CONTRACT-FACTS §4.1) -- anything else in a declaration
# is dropped rather than written, so a bad/typo'd column name can never point at an arbitrary attribute.
_ALLOWED_TARGET_COLUMNS = {"target_host", "target_port", "target_url"}

logger = logging.getLogger(__name__)

_DEFAULT_JSON = Path(__file__).parent / "faction_vulnerabilities.json"

# FACTION SeverityId (see convert_findings.py): Critical5 High4 Medium3 Low2 Informational0.
_SEV_BY_ID = {
    5: Severity.critical,
    4: Severity.high,
    3: Severity.medium,
    2: Severity.low,
    1: Severity.low,
    0: Severity.info,
}

_DEFAULT_TYPES = [
    ("Internal", "internal", "#7c3aed", 0),
    ("External", "external", "#0284c7", 1),
    ("Web App", "web-app", "#16a34a", 2),
    ("Device / Mobile", "device-mobile", "#ea580c", 3),
]

_BUILTIN_LABELS = {
    "COMPANY_NAME": "Company name",
    "ENGAGEMENT_NAME": "Engagement name",
    "TARGET_HOST": "Target host",
    "TARGET_PORT": "Target port",
    "TARGET_URL": "Target URL",
    "ASSESSOR": "Assessor",
    "TODAY": "Today's date",
    "START_DATE": "Start date",
    "END_DATE": "End date",
    "SEVERITY": "Finding severity",
}
_FINDING_SCOPED = {"TARGET_HOST", "TARGET_PORT", "TARGET_URL", "SEVERITY"}


def seed_assessment_types(session) -> int:
    added = 0
    for name, slug, color, order in _DEFAULT_TYPES:
        if session.scalar(select(AssessmentType).where(AssessmentType.slug == slug)) is None:
            session.add(AssessmentType(name=name, slug=slug, color=color, default_order=order))
            added += 1
    return added


def seed_builtin_variables(session) -> int:
    added = 0
    for key in BUILTIN_KEYS:
        if session.scalar(select(TemplateVariable).where(TemplateVariable.key == key)) is None:
            scope = VariableScope.finding if key in _FINDING_SCOPED else VariableScope.engagement
            session.add(
                TemplateVariable(
                    key=key,
                    label=_BUILTIN_LABELS.get(key, key.title()),
                    scope=scope,
                    value_type=VariableType.str_,
                    builtin=True,
                )
            )
            added += 1
    return added


def _block_html(doc: dict) -> str:
    # Cache raw HTML with tokens left intact (resolution happens at render time).
    return render_html.render_block(doc)


def import_vuln_templates(session, json_path: str | Path | None = None) -> int:
    """Import FACTION default vulns into the template library. Idempotent by name.

    Each record's ``Description`` (embedded HTML + ``# Description``/``# Impact``/
    ``# Replication Steps`` markers) and ``Recommendation`` (embedded HTML) are parsed into real
    ``description``/``details``/``remediation`` ProseMirror blocks by
    ``faction_parse.build_template_blocks`` -- see that module for the section-mapping and foreign-
    token-normalization rules.
    """
    path = Path(json_path) if json_path else _DEFAULT_JSON
    if not path.exists():
        return 0
    records = json.loads(path.read_text())
    added = 0
    for rec in records:
        name = rec.get("Name", "").strip()
        if not name:
            continue
        if session.scalar(select(VulnerabilityTemplate).where(VulnerabilityTemplate.name == name)):
            continue
        content_json = faction_parse.build_template_blocks(
            rec.get("Description", ""), rec.get("Recommendation", "")
        )
        content_html = {block: _block_html(doc) for block, doc in content_json.items()}
        session.add(
            VulnerabilityTemplate(
                name=name,
                category=rec.get("CategoryName"),
                default_severity=_SEV_BY_ID.get(int(rec.get("SeverityId", 3)), Severity.medium),
                content_json=content_json,
                content_html=content_html,
                active=bool(rec.get("Active", True)),
            )
        )
        added += 1
    return added


_LOTEK_JSON = Path(__file__).parent / "lotek_vulnerabilities.json"
_DEFAULT_VULN_MAP_JSON = Path(__file__).parent / "lotek_vuln_map.json"


def seed_vuln_map(session, json_path: str | Path | None = None) -> int:
    """Seed ``FractionVulnMap`` rows mapping a lotek scan-finding signature to a library
    ``VulnerabilityTemplate``, so the promote step auto-selects the polished write-up instead of
    bridging the raw finding verbatim (``EngagementFinding.from_lotek_finding``'s plain-text fallback).

    Ported from lotek core's ``ensure_vuln_map_seed`` (``src/app/seed.py``) now that promotion is
    entirely Fraction's own concern and the mapping table (``FractionVulnMap``) lives here instead of
    the deleted lotek ``VulnMap``. MUST run AFTER the library import (``import_vuln_templates``) --
    ``seed_defaults`` below calls it in that order -- so the names it looks up already exist.

    Idempotent on the match-key ``(source, title_pattern, dedupe_prefix)``: a prior run's row for that
    exact key short-circuits it on later boots, so a template later renamed/retired can't silently break
    an already-seeded mapping. On a FRESH lookup, zero active templates named ``name`` is a hard failure
    (raises) -- a missing library entry is model drift, not something to paper over with a silent skip.
    Multiple active templates sharing the name (``VulnerabilityTemplate.name`` has no unique constraint)
    resolve deterministically to the LOWEST id, logged as a warning so the ambiguity is visible without
    blocking boot.

    ``json_path`` defaults to ``fraction/seed/lotek_vuln_map.json`` (the 11-entry set transcribed
    verbatim from lotek's ``VULN_MAP_SEED``); tests may pass a substitute path to exercise the defensive
    zero-match/ambiguous-match paths without touching the real seed data.
    """
    path = Path(json_path) if json_path else _DEFAULT_VULN_MAP_JSON
    if not path.exists():
        return 0
    specs = json.loads(path.read_text())
    added = 0
    for spec in specs:
        name = spec["name"]
        source = spec.get("source")
        title_pattern = spec.get("title_pattern")
        dedupe_prefix = spec.get("dedupe_prefix")
        existing = session.scalar(
            select(FractionVulnMap).where(
                FractionVulnMap.source.is_(source) if source is None else FractionVulnMap.source == source,
                FractionVulnMap.title_pattern.is_(title_pattern)
                if title_pattern is None
                else FractionVulnMap.title_pattern == title_pattern,
                FractionVulnMap.dedupe_prefix.is_(dedupe_prefix)
                if dedupe_prefix is None
                else FractionVulnMap.dedupe_prefix == dedupe_prefix,
            )
        )
        if existing is not None:
            continue  # already seeded this match-key (idempotent across restarts)
        templates = session.scalars(
            select(VulnerabilityTemplate).where(
                VulnerabilityTemplate.name == name,
                VulnerabilityTemplate.active.is_(True),
            )
        ).all()
        if not templates:
            raise RuntimeError(
                f"fraction VulnMap seed: no active fraction_vuln_templates row named {name!r} -- "
                "import_vuln_templates may not have run yet, or this vuln-DB entry was renamed/removed "
                "from lotek_vulnerabilities.json"
            )
        if len(templates) > 1:
            logger.warning(
                "fraction VulnMap seed: %d active fraction_vuln_templates rows named %r; "
                "using the lowest id (%s) for determinism",
                len(templates), name, min(t.id for t in templates),
            )
        template = min(templates, key=lambda t: t.id)
        session.add(
            FractionVulnMap(
                source=source,
                title_pattern=title_pattern,
                dedupe_prefix=dedupe_prefix,
                template_id=template.id,
                created_by="seed",
            )
        )
        added += 1
    return added


_DEFAULT_REPORT_VARIABLES_JSON = Path(__file__).parent / "report_variables.json"


def seed_report_variables(session, json_path: str | Path | None = None) -> int:
    """Seed/refresh the declarative fact -> report-variable mapping (``TemplateVariable.from_facts`` /
    ``.target_column``, CONTRACT-FACTS §4.1/§4.3) from ``fraction/seed/report_variables.json``.

    This is the DATA half of "tools are entirely defined in the DB, not hardcoded edge cases in the
    codebase": which facts feed ``{{AFFECTED}}``/``{{DOMAIN}}``/etc, and how, lives in this JSON and on
    the ``fraction_variables`` row -- never in a per-source Python dispatch table.

    Unlike ``seed_vuln_map`` (pure idempotent insert), this is UPGRADE-safe by design: a key missing
    from the DB gets a new ``TemplateVariable`` row (``builtin=True``, ``value_type=str``); a key that
    already exists has ONLY its ``from_facts``/``target_column`` overwritten, so a shipped mapping fix
    (e.g. adding a new fact source for ``AFFECTED``) reaches an already-seeded database on the next
    boot -- ``label``/``scope``/``value_type``/``builtin`` are left alone since an operator may have
    hand-edited them via the variables UI.

    Best-effort (never raises, never blocks a boot): a non-list ``from_facts`` or a ``target_column``
    outside ``{"target_host", "target_port", "target_url"}`` is dropped with a ``logger.debug``, not an
    exception. A record with no ``key`` is skipped the same way.
    """
    path = Path(json_path) if json_path else _DEFAULT_REPORT_VARIABLES_JSON
    if not path.exists():
        return 0
    try:
        records = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        logger.debug("report_variables seed: %s is missing or not valid JSON, skipping", path)
        return 0
    touched = 0
    for rec in records:
        if not isinstance(rec, dict):
            continue
        key = rec.get("key")
        if not key or not isinstance(key, str):
            logger.debug("report_variables seed: dropping record with no/invalid key: %r", rec)
            continue
        from_facts = rec.get("from_facts") or []
        if not isinstance(from_facts, list):
            logger.debug("report_variables seed: %r has non-list from_facts, dropping it", key)
            from_facts = []
        target_column = rec.get("target_column")
        if target_column is not None and target_column not in _ALLOWED_TARGET_COLUMNS:
            logger.debug(
                "report_variables seed: %r declares disallowed target_column %r, dropping it",
                key, target_column,
            )
            target_column = None
        existing = session.scalar(select(TemplateVariable).where(TemplateVariable.key == key))
        if existing is None:
            scope_name = rec.get("scope", "finding")
            try:
                scope = VariableScope(scope_name)
            except ValueError:
                logger.debug(
                    "report_variables seed: %r has unknown scope %r, defaulting to finding",
                    key, scope_name,
                )
                scope = VariableScope.finding
            session.add(
                TemplateVariable(
                    key=key,
                    label=rec.get("label") or key.replace("_", " ").title(),
                    scope=scope,
                    value_type=VariableType.str_,
                    from_facts=from_facts,
                    target_column=target_column,
                    # NOT builtin=True: ``builtin`` is pinned elsewhere (test_smoke.py::test_seed) to
                    # mean exactly the structural, resolver.build_context-computed BUILTIN_KEYS set
                    # (COMPANY_NAME/TARGET_HOST/.../SEVERITY). These are a separate, declarative,
                    # fact-mapped vocabulary layered on top -- still shipped defaults, but not part of
                    # that fixed structural set, so they stay builtin=False (still editable/overridable
                    # the same as any other TemplateVariable row).
                    builtin=False,
                )
            )
        else:
            existing.from_facts = from_facts
            existing.target_column = target_column
        touched += 1
    return touched


def seed_defaults(session, *, import_library: bool = True) -> dict[str, int]:
    """Seed assessment types, built-in variables, and (optionally) the vuln-template library
    (the FACTION default set + lotek's AD/network entries) plus its VulnMap resolution seed."""
    templates = 0
    vuln_map = 0
    if import_library:
        templates = import_vuln_templates(session)                 # FACTION default library
        templates += import_vuln_templates(session, _LOTEK_JSON)   # lotek AD/network vuln-DB entries
        vuln_map = seed_vuln_map(session)                           # lotek finding -> template mapping
    result = {
        "assessment_types": seed_assessment_types(session),
        "builtin_variables": seed_builtin_variables(session),
        "report_variables": seed_report_variables(session),
        "templates": templates,
        "vuln_map": vuln_map,
    }
    return result
