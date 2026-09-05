"""scribble/promote.py — promote lotek scan findings into a Scribble engagement.

Ported from the deleted lotek ``app/api_v1_scribble.py`` (``scribble_promote_job`` /
``scribble_add_finding`` / ``_resolve_vuln_template`` / ``_get_or_create_parent``), rewired to the host
contract (CONTRACT.md §1, CONTRACT-FACTS.md §4): every scan finding this module sees is a
``host_contract.FindingDTO``-shaped object — duck-typed, this module never imports lotek — and the vuln-
DB mapping table is Scribble's OWN ``ScribbleVulnMap`` (a real foreign key: Scribble always has its own
``scribble_vuln_templates``, so unlike the deleted core-side ``VulnMap`` there is no capability gating
here at all — ``parent_id``/``source_finding_id``/``target_host`` are unconditional columns).

Report-variable values (``{{AFFECTED}}``, ``{{DOMAIN}}``, …) are computed per finding via
``scribble.facts.resolve_variables``/``synthesize_parent_variables`` against the DB-declared
``TemplateVariable.from_facts`` rules (CONTRACT-FACTS.md §4.1/§4.2) and stored on
``EngagementFinding.variables`` — plus, for a ``target_column``-declared key (``TARGET_HOST``/
``TARGET_URL``/…), onto the matching column itself. Nothing in this file names a tool or branches on
``dto.source`` — the only source-shaped thing here is ``ScribbleVulnMap``, which is a DATA lookup an
operator curates, not a Python branch.
"""

from __future__ import annotations

import fnmatch
import uuid
from typing import Any

from sqlalchemy import select

from scribble.dispositions import confidence_from_dto, snapshot_source_facts, status_from_dto
from scribble.facts import resolve_variables, synthesize_parent_variables
from scribble.metadata import (
    REF_SOURCE_SCAN,
    REF_SOURCE_TEMPLATE,
    derive_owasp,
    merge_references,
    normalize_cve_ids,
    normalize_cwe_ids,
)
from scribble.models import EngagementFinding, ScribbleVulnMap, TemplateVariable, VulnerabilityTemplate

# Columns a declaration may ALSO write directly onto the created row (besides ``variables``). Mirrors
# ``TemplateVariable.target_column``'s own allowlist (models.py) -- enforced again here, at the one place
# that actually performs the write, so a hand-edited row can never steer a write at an arbitrary column.
_ALLOWED_TARGET_COLUMNS = frozenset({"target_host", "target_port", "target_url"})


def _match_title(pattern: str, title: str) -> bool:
    """Case-insensitive glob match of a ``ScribbleVulnMap.title_pattern`` against a finding's title."""
    return fnmatch.fnmatchcase(title.lower(), pattern.lower())


def resolve_vuln_template(
    db: Any, *, source: str | None, title: str | None, dedupe_key: str | None
) -> int | None:
    """Resolve a scan finding to a ``ScribbleVulnMap.template_id``, most-specific-first: ``dedupe_prefix``
    (longest match wins, ties broken by lowest id) > ``source`` + glob ``title_pattern`` > ``source``
    alone. ``None`` when nothing matches. Does NOT re-check the resolved template is still active/exists
    -- callers that need the row (this module's own ``_matched_template``, and
    ``scribble/api_pat.py::scribble_resolve_template``) re-check themselves, since a stale mapping should
    resolve to "no template" rather than raise.
    """
    if dedupe_key:
        rows = (
            db.execute(
                select(ScribbleVulnMap)
                .where(ScribbleVulnMap.dedupe_prefix.isnot(None))
                .order_by(ScribbleVulnMap.id)
            )
            .scalars()
            .all()
        )
        best = None
        for m in rows:
            if dedupe_key.startswith(m.dedupe_prefix) and (
                best is None or len(m.dedupe_prefix) > len(best.dedupe_prefix)
            ):
                best = m
        if best is not None:
            return best.template_id
    if source and title:
        rows = (
            db.execute(
                select(ScribbleVulnMap).where(
                    ScribbleVulnMap.source == source, ScribbleVulnMap.title_pattern.isnot(None)
                )
            )
            .scalars()
            .all()
        )
        for m in rows:
            if _match_title(m.title_pattern, title):
                return m.template_id
    if source:
        m = db.execute(
            select(ScribbleVulnMap).where(
                ScribbleVulnMap.source == source,
                ScribbleVulnMap.title_pattern.is_(None),
                ScribbleVulnMap.dedupe_prefix.is_(None),
            )
        ).scalars().first()
        if m is not None:
            return m.template_id
    return None


def _matched_template(db: Any, dto: Any) -> VulnerabilityTemplate | None:
    """The active, human-authored ``VulnerabilityTemplate`` this finding resolves to, or ``None`` (no
    mapping, a mapping to a deleted/retired template, a MACHINE-authored one, or nothing to match on).

    This is the AUTOMATIC path: promotion instantiates whatever a ``ScribbleVulnMap`` rule matches, with
    no human choosing the template and no tenancy check — the library is a single shared table and
    ``from_template`` copies ``content_json`` verbatim into a client-facing finding. A write-scoped PAT
    can already install a global vuln-map rule, so if it could also AUTHOR the content, agent-written
    prose would reach another tenant's deliverable unread. Machine-authored templates are therefore
    excluded HERE — they stay explicitly instantiable by id (a deliberate act by someone who already
    holds the destination engagement), just never silently adopted.
    """
    template_id = resolve_vuln_template(
        db,
        source=getattr(dto, "source", None),
        title=getattr(dto, "title", None),
        dedupe_key=getattr(dto, "dedupe_key", None),
    )
    if template_id is None:
        return None
    template = db.get(VulnerabilityTemplate, template_id)
    if template is None or not template.active:
        return None
    return None if getattr(template, "machine_authored", False) else template


def _load_declarations(db: Any) -> list[tuple[str, str | None, list]]:
    """The ``scribble_variables`` fact-mapping registry, read fresh for this promote call: one
    ``(key, target_column, rules)`` tuple per row that declares at least one rule. Best effort: a row
    whose ``from_facts`` isn't a JSON list contributes no rules (dropped, never raises); a
    ``target_column`` outside the allowlist is dropped too (kept as ``None``, so the key's ``variables``
    entry is still computed, just never written onto a column).
    """
    rows = db.execute(
        select(TemplateVariable.key, TemplateVariable.target_column, TemplateVariable.from_facts)
    ).all()
    out: list[tuple[str, str | None, list]] = []
    for key, target_column, from_facts in rows:
        rules = from_facts if isinstance(from_facts, list) else []
        if not rules:
            continue
        col = target_column if target_column in _ALLOWED_TARGET_COLUMNS else None
        out.append((str(key), col, rules))
    return out


def _target_overrides(
    variables: dict[str, str], declarations: list[tuple[str, str | None, list]]
) -> dict[str, str]:
    """The subset of ``variables`` that a declaration also wants written onto a real
    ``EngagementFinding`` column (``target_host``/``target_port``/``target_url``), keyed by that column
    name so it can be splatted straight into the constructor ``overrides``."""
    overrides: dict[str, str] = {}
    for key, target_column, _rules in declarations:
        if not target_column:
            continue
        value = variables.get(key)
        if value:
            overrides[target_column] = value
    return overrides


def _source_overrides(dto: Any) -> dict[str, Any]:
    """The DTO-derived fields promote stamps on EVERY promoted row, whether or not a template matched
    (map #616 / #617): ``confidence``/``status`` mapped onto their typed columns (which previously sat
    silently at ``medium``/``new`` because promote never mapped them), the structured metadata
    ``cve_ids``/``cwe_ids``/``owasp_categories`` (#625 -- ``cve_ids`` from the scalar ``DTO.cve``,
    ``cwe_ids`` from ``DTO.facts["cwe"]`` with NO DTO widening, ``owasp_categories`` DERIVED from the CWEs
    via the offline map), and the FULL DTO captured verbatim in ``source_facts`` so ``EngagementFinding``
    is a lossless superset of the scan finding. ``references`` is stamped separately (it needs the matched
    template too -- see ``_reference_override``).

    Applied via the constructor ``overrides``, so it wins on BOTH paths -- ``from_template`` (which builds
    the row from a library template and would otherwise leave these at defaults / drop the source values)
    and ``from_lotek_finding``. Every derivation is DEFENSIVE (unknown value -> empty/default;
    ``scribble.metadata``/``scribble.dispositions``) -- a scan must never break a promote."""
    cwe_ids = normalize_cwe_ids((getattr(dto, "facts", None) or {}).get("cwe"))
    return {
        "confidence": confidence_from_dto(dto),
        "status": status_from_dto(dto),
        "cve_ids": normalize_cve_ids(getattr(dto, "cve", None)),
        "cwe_ids": cwe_ids,
        "owasp_categories": derive_owasp(cwe_ids),
        "source_facts": snapshot_source_facts(dto),
    }


def _reference_override(dto: Any, template: VulnerabilityTemplate | None) -> list[dict]:
    """The structured ``references`` for a promoted row (#624): the UNION of the matched library
    template's ``references`` (source ``template``) and the scan ``DTO.references`` (source ``scan``),
    deduped by normalized url with the template winning a collision. No template -> scan refs only. An
    operator's later edits (source ``author``) are never merged here -- re-promote is fill-NULL-only and
    skips an existing row, so an operator suppress/edit is never clobbered (#617 Q5 / #624 operator-wins)."""
    template_refs = template.references if template is not None else []
    return merge_references(
        template_refs, getattr(dto, "references", None),
        sources=(REF_SOURCE_TEMPLATE, REF_SOURCE_SCAN),
    )


def _get_or_create_parent(
    db: Any, *, engagement_id: int, template: VulnerabilityTemplate, actor: str | None, order_index: int
) -> tuple[EngagementFinding, bool]:
    """Find-or-create the ONE parent ``EngagementFinding`` this template's matched instances nest under.

    Idempotency key: ``(engagement_id, template_id, parent_id IS NULL, source_finding_id IS NULL,
    title == template.name)`` -- the null parent_id/source_finding_id pair is exactly what distinguishes
    a parent WE created here from an ordinary child/flat row (every row promoted from a real scan finding
    always stamps ``source_finding_id``). Returns ``(parent, created_bool)``.
    """
    existing = (
        db.execute(
            select(EngagementFinding)
            .where(
                EngagementFinding.engagement_id == engagement_id,
                EngagementFinding.template_id == template.id,
                EngagementFinding.parent_id.is_(None),
                EngagementFinding.source_finding_id.is_(None),
                EngagementFinding.title == template.name,
            )
            .order_by(EngagementFinding.id)
        )
        .scalars()
        .first()
    )
    if existing is not None:
        return existing, False
    parent = EngagementFinding.from_template(
        template,
        engagement_id=engagement_id,
        group_id=None,
        parent_id=None,
        order_index=order_index,
        created_by=actor,
    )
    db.add(parent)
    db.flush()  # assign parent.id so children can set parent_id in the same transaction
    return parent, True


def promote_one(
    db: Any, *, engagement: Any, group: Any, dto: Any, actor_username: str | None, order_index: int
):
    """Promote ONE lotek scan finding (``dto``) into ``engagement`` -- always FLAT (attached to ``group``,
    or top-level when ``group`` is ``None``); parent/child nesting only ever happens in bulk
    ``promote_job``. Resolves a library template via ``ScribbleVulnMap``; falls back to bridging the raw
    finding verbatim (``from_lotek_finding``) when nothing matches. Report-variable values are computed
    from ``dto.facts``/attributes via the DB-declared mapping and stored on
    ``EngagementFinding.variables`` -- plus, for any ``target_column``-declared key, onto the matching
    column. Adds the created row to ``db`` (a caller may ``db.add`` it again defensively -- a harmless
    no-op on an already-pending object).

    Dedup is the CALLER's job (``scribble/api_pat.py::scribble_add_finding`` checks
    ``source_finding_id`` before ever calling this) -- this function does not re-check.
    """
    declarations = _load_declarations(db)
    variables = resolve_variables(dto, declarations)
    overrides: dict[str, Any] = {
        "engagement_id": engagement.id,
        "group_id": group.id if group is not None else None,
        "order_index": order_index,
        "created_by": actor_username,
        "source_finding_id": getattr(dto, "id", None),
        "variables": variables,
    }
    overrides.update(_target_overrides(variables, declarations))
    overrides.update(_source_overrides(dto))

    template = _matched_template(db, dto)
    overrides["references"] = _reference_override(dto, template)
    finding = (
        EngagementFinding.from_template(template, **overrides)
        if template is not None
        else EngagementFinding.from_lotek_finding(dto, **overrides)
    )
    db.add(finding)
    return finding


def promote_job(db: Any, *, engagement: Any, findings: list, actor_username: str | None) -> dict:
    """Bulk-promote a lotek scan job's findings (host ``FindingDTO``s) into ``engagement``.

    Findings that resolve to the SAME library template are grouped under ONE parent
    ``EngagementFinding`` (the vuln-DB write-up, built from the template) with each scan finding becoming
    a CHILD (``parent_id`` set; its own ``target_host``/``variables`` derived from its OWN facts, so the
    per-host rows in a report show real per-host evidence, never a copy of the parent). A finding that
    matches no template is bridged verbatim (``from_lotek_finding``) and stays flat/ungrouped -- there is
    no reliable signature to group unmapped findings by.

    Idempotent: re-running skips any finding already promoted into this engagement (precise
    ``source_finding_id`` dedup, plus a legacy title fallback for rows promoted before that column
    existed). Never touches the lotek ``Job`` -- recording the promotion is the host contract's own write
    (``host.mark_job_promoted``, called by ``scribble/api_pat.py`` in a separate transaction after this
    one commits).

    Returns ``{"promoted": int, "skipped": int, "parents": int}``.
    """
    declarations = _load_declarations(db)

    promoted_source_ids = {
        f.source_finding_id for f in engagement.findings if f.source_finding_id is not None
    }
    # Legacy fallback: rows promoted before ``source_finding_id`` existed carry NULL there, so the
    # precise dedup above can't see them -- guard those by title instead (scoped to null-source rows,
    # which is also where a PARENT row itself always lives).
    legacy_titles = {f.title for f in engagement.findings if f.source_finding_id is None}
    # Pre-existing rows keyed by their source finding id, for the re-promote source_facts refresh below.
    existing_by_source = {
        f.source_finding_id: f for f in engagement.findings if f.source_finding_id is not None
    }
    siblings = [f for f in engagement.findings if f.group_id is None]

    order_index = len(siblings)
    # Keyed by ``template.id``, a UUIDv7 since the scribble UUID-PK migration — the annotations said
    # ``int`` (leftover from the int-PK era), which typechecks as a real error against ``template.id``.
    parents_by_template: dict[uuid.UUID, EngagementFinding] = {}
    parent_children: dict[uuid.UUID, list[Any]] = {}
    promoted = 0
    skipped = 0
    parents_created = 0

    for dto in findings:
        title = getattr(dto, "title", None) or "Untitled"
        dto_id = getattr(dto, "id", None)
        if dto_id in promoted_source_ids or title in legacy_titles:
            # Re-promote: refresh the verbatim snapshot on the already-promoted row (source truth), but
            # touch NO typed column -- an operator edit is never clobbered (#617 Q5, fill-NULL-only). Only
            # a precise source_finding_id match identifies the row; the legacy-title fallback cannot, so it
            # just skips. `existing_by_source` holds pre-existing rows only, so a within-run duplicate id
            # (not a real re-promote) correctly finds nothing to refresh.
            existing = existing_by_source.get(dto_id) if dto_id is not None else None
            if existing is not None:
                existing.source_facts = snapshot_source_facts(dto)
            skipped += 1
            continue

        variables = resolve_variables(dto, declarations)
        overrides: dict[str, Any] = {
            "engagement_id": engagement.id,
            "group_id": None,
            "order_index": order_index,
            "created_by": actor_username,
            "source_finding_id": dto_id,
            "variables": variables,
        }
        overrides.update(_target_overrides(variables, declarations))
        overrides.update(_source_overrides(dto))

        template = _matched_template(db, dto)
        overrides["references"] = _reference_override(dto, template)
        if template is not None:
            parent = parents_by_template.get(template.id)
            if parent is None:
                parent, created = _get_or_create_parent(
                    db,
                    engagement_id=engagement.id,
                    template=template,
                    actor=actor_username,
                    order_index=order_index,
                )
                order_index += 1
                parents_by_template[template.id] = parent
                parent_children[template.id] = []
                if created:
                    parents_created += 1
            overrides["parent_id"] = parent.id
            overrides["order_index"] = order_index
            finding = EngagementFinding.from_template(template, **overrides)
            parent_children[template.id].append(dto)
        else:
            finding = EngagementFinding.from_lotek_finding(dto, **overrides)

        order_index += 1
        db.add(finding)
        if dto_id is not None:
            promoted_source_ids.add(dto_id)
        promoted += 1

    # One deterministic synthesis pass per parent, once every one of its children is known.
    for template_id, parent in parents_by_template.items():
        children = parent_children.get(template_id) or []
        if children:
            parent.variables = synthesize_parent_variables(children, declarations)

    return {"promoted": promoted, "skipped": skipped, "parents": parents_created}
