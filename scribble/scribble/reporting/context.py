"""Build the ``ReportContext`` for an engagement — the single source both renderers consume.

Ordering rules (PLAN.md §4 "Grouping & ordering UX"):
- Groups render in ``FindingGroup.order_index`` order; a synthetic "Ungrouped" bucket (if any findings
  have no group) renders last.
- Within a group: ``auto_severity`` sorts worst-first then by ``order_index``; ``manual`` sorts by
  ``order_index`` only.
- ``include_in_report`` on group / finding / artifact filters what enters the context.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy.orm import object_session

from scribble import findings_service
from scribble.content import render_html
from scribble.enums import OrderMode, Severity, risk_rating, severity_rank
from scribble.templating import build_context, build_full_context, make_var_resolver


@dataclass
class ArtifactCtx:
    id: int
    kind: str
    filename: str
    caption: str
    content_type: str | None
    # WHERE THE BYTES ARE, not necessarily a path: either a disk path relative to ``artifact_root`` or
    # an ``obj:<uuid>`` reference into the core object store (``Artifact.storage_path``, carried
    # through verbatim). The paired ``artifact_bytes`` reader resolves whichever it is, so a renderer
    # never has to know, and this stays a plain copy of the column rather than a second opinion on it.
    storage_path: str
    # What the row recorded for this file, so a renderer can decide whether to carry its bytes WITHOUT
    # reading them first (``render_html``'s inlining budget). Advisory only -- the bytes on disk stay the
    # authority, and the field is additive + defaulted, so an existing consumer is unaffected.
    byte_size: int | None = None
    # ADDITIVE (ext#117): the report-wide figure number, assigned by :func:`number_figures` in document
    # order. ``None`` only for a context assembled by hand in a test that never called it.
    figure_number: int | None = None


@dataclass
class FindingCtx:
    id: int
    title: str
    severity: str
    cvss_score: float | None
    cvss_vector: str | None
    target_host: str | None
    target_port: str | None
    target_url: str | None
    blocks_html: dict[str, str]  # block_name -> rendered, variable-resolved HTML
    artifacts: list[ArtifactCtx]
    # Nested per-host instances of the same vuln TYPE (``EngagementFinding.parent_id`` -> this finding's
    # id). Populated only for a PARENT finding; a finding with no children leaves this ``[]`` and
    # renders exactly as before nesting existed. Children themselves are never top-level (they don't
    # appear in ``GroupCtx.findings`` once nested) -- see ``_nest_findings``.
    children: list[FindingCtx] = field(default_factory=list)
    # The report-variable overlay for THIS finding (``EngagementFinding.variables``, filled by promote --
    # see ``scribble.facts``/``scribble.promote``). Also the source of ``facts_line`` (a tight per-host
    # evidence line built from these values, replacing a truncated copy of the parent's description --
    # see ``render_html._child_summary_text``/``render_docx`` equivalent).
    variables: dict = field(default_factory=dict)
    facts_line: str = ""


@dataclass
class DiagramCtx:
    """A linked vector attack-path diagram, ready to embed (ext#48). ``embed_html`` is a self-contained
    HTML snapshot (vector's ``export.html``) — the renderer puts it straight into a sandboxed iframe,
    never parses or re-fetches it. See ``scribble.models.EngagementDiagram`` for why this is a stored
    snapshot rather than a live cross-extension fetch."""

    id: int
    diagram_ref: str
    caption: str
    embed_html: str
    # ADDITIVE (ext#117): see ``ArtifactCtx.figure_number``. A diagram is a figure like any other — it
    # is numbered in the same continuous sequence so "Figure 4" means one thing across the report.
    figure_number: int | None = None


@dataclass
class GroupCtx:
    id: int | None
    name: str
    type_slug: str | None
    color: str | None
    findings: list[FindingCtx]


@dataclass
class SeverityRollup:
    counts: dict[str, int]
    total: int
    overall: str


@dataclass
class ChecklistItemCtx:
    section: str | None
    text: str
    guidance: str | None
    framework: str | None
    control_ref: str | None
    status: str
    bucket: str  # satisfied | deficient | not_applicable | open
    bucket_label: str
    note: str | None
    finding_id: int | None
    finding_title: str | None  # resolved from the engagement's findings for a coverage cross-link


@dataclass
class ChecklistCtx:
    id: int
    name: str
    kind: str  # coverage | reminder | compliance
    rollup: dict[str, int]  # bucket -> count
    total: int
    items: list[ChecklistItemCtx]


@dataclass
class ActivityEntry:
    """One row of the engagement activity trail (lotek#442) — a timestamped, report-relevant action
    (finding added, evidence uploaded, diagram created), assembled from scribble's own TimestampMixin
    ``created_at`` columns. No cross-seam sourcing; this is scribble's view of its own engagement."""
    timestamp: str  # display string, UTC (empty if the row has no created_at)
    kind: str        # "engagement" | "finding" | "evidence" | "diagram"
    summary: str     # human-readable, e.g. "Finding added: SMB signing not required"


@dataclass
class ReportContext:
    engagement_id: int
    engagement_name: str
    company_name: str
    client_name: str | None
    scope_type: str | None
    start_date: str | None
    end_date: str | None
    groups: list[GroupCtx] = field(default_factory=list)
    rollup: SeverityRollup | None = None
    checklists: list[ChecklistCtx] = field(default_factory=list)
    variables: dict[str, str] = field(default_factory=dict)
    # ADDITIVE extension to this frozen contract (2026-08-17, ext#40): engagement-level evidence —
    # artifacts attached to the ENGAGEMENT with no ``finding_id``. Until this field existed the renderers
    # could only reach ``finding.artifacts``, so an upload with no ``finding_id`` was stored, answered
    # 201 with a URL, and then could never appear in any deliverable — a silent, invisible loss of client
    # evidence. Existing consumers are unaffected: new field, defaults empty, nothing reordered or
    # renamed. Rendered by ``render_html``'s Evidence appendix block.
    artifacts: list[ArtifactCtx] = field(default_factory=list)
    # ADDITIVE (2026-08-19, ext#48): linked vector attack-path diagrams. Defaults empty — an engagement
    # with none renders BYTE-IDENTICALLY to before this field existed (see
    # ``render_html._render_diagrams``/``_render_document``'s empty-block filter, and
    # ``tests/test_report_attack_path.py`` which pins that guarantee).
    diagrams: list[DiagramCtx] = field(default_factory=list)
    # Generated executive-summary narrative paragraph (see ``_build_narrative``) -- synthesized from
    # ``rollup`` + the worst top-level finding titles, not authored by hand.
    narrative: str = ""
    # ADDITIVE (lotek#442): OPT-IN engagement activity trail, rendered as an appendix ONLY when a
    # template includes the ``activity_log`` block (the "checkbox"). Built from the engagement's own
    # created_at timestamps in ``build_report_context``. Defaults empty — an engagement renders
    # identically to before unless a template opts the block in.
    activity_log: list[ActivityEntry] = field(default_factory=list)


def _order_findings(group_findings, order_mode: OrderMode):
    included = [f for f in group_findings if f.include_in_report]
    if order_mode == OrderMode.auto_severity:
        return sorted(included, key=lambda f: (severity_rank(f.severity), f.order_index))
    return sorted(included, key=lambda f: f.order_index)


_FACTS_LINE_KEYS = ("DOMAIN", "AFFECTED", "TARGET_URL")


def _facts_line(variables: dict) -> str:
    """A tight, single-line evidence summary for a per-host child row, built from THIS finding's own
    report-variable overlay (``EngagementFinding.variables`` — see ``scribble.facts``/``scribble.promote``),
    never from its content blocks: every child promoted under the same vuln-DB template shares the
    template's ``content_json`` verbatim with its parent, so summarizing ``blocks_html`` would repeat the
    parent's write-up for every host instead of showing what's actually different about this one. Reads
    only generic variable KEYS (``DOMAIN``/``AFFECTED``/``TARGET_URL``, the same vocabulary every
    ``{{TOKEN}}`` in a write-up already uses) — no tool/source name is referenced here."""
    if not variables:
        return ""
    domain = str(variables.get("DOMAIN") or "").strip()
    affected = str(variables.get("AFFECTED") or "").strip()
    if domain and affected and affected != domain:
        return f"{domain} — {affected}"
    for key in _FACTS_LINE_KEYS:
        value = str(variables.get(key) or "").strip()
        if value:
            return value
    return ""


def _artifact_ctxs(artifacts, *, engagement_id=None) -> list[ArtifactCtx]:
    """``ArtifactCtx``\\s for an evidence gallery, in board order, honoring ``include_in_report`` and
    skipping ``inline``-placed artifacts (those are already embedded in a content block's HTML).

    ``engagement_id``, when given, is a defence-in-depth cross-check (ext#52): a gallery must never
    render an artifact whose ``engagement_id`` differs from the one it is being built for, even if one
    somehow slipped past the write-time tenancy check. Every legitimate row already carries the right
    ``engagement_id`` (routes set it to the finding's own engagement), so this can never drop a real
    artifact -- it only guards against the write-time check having a gap.
    """
    return [
        ArtifactCtx(
            id=a.id,
            kind=a.kind.value,
            filename=a.filename,
            caption=a.caption or "",
            content_type=a.content_type,
            storage_path=a.storage_path,
            byte_size=a.byte_size,
        )
        for a in sorted(artifacts, key=lambda a: (a.order_index, a.id))
        if a.include_in_report
        and a.placement.value == "attached"
        and (engagement_id is None or a.engagement_id == engagement_id)
    ]


def _diagram_ctxs(diagrams) -> list[DiagramCtx]:
    """``DiagramCtx``\\s for the report's Attack Paths block, in board order, honoring
    ``include_in_report`` and skipping any row whose snapshot never got attached (``embed_html`` empty
    — e.g. a link record created before the snapshot POST completed)."""
    return [
        DiagramCtx(
            id=d.id,
            diagram_ref=d.diagram_ref or "",
            caption=d.caption or "",
            embed_html=d.embed_html,
        )
        for d in sorted(diagrams, key=lambda d: (d.order_index, d.id))
        if d.include_in_report and d.embed_html
    ]


FIGURE_SEPARATOR = " — "  # "Figure 3 — Payload firing in the browser"

# What an UNCAPTIONED attack-path diagram's figure caption says. Lives here, on the contract both
# renderers consume, because the two deliverables must print one figure number under ONE caption --
# the .docx used to fall back to the model's own ``meta.title`` while the HTML said "Attack path",
# giving the same figure two names.
DIAGRAM_CAPTION_FALLBACK = "Attack path"


def figure_label(number: int | None) -> str:
    """``"Figure 3"`` for a numbered figure, ``""`` for an un-numbered one.

    Single-sourced here rather than formatted in each renderer, because ext#117's requirement is that
    the two deliverables agree — a figure that is "Figure 3" on screen and unnumbered in Word is worse
    than neither. Same reason :func:`number_figures` assigns the numbers here and not in a renderer."""
    return "" if number is None else f"Figure {number}"


def figure_caption(number: int | None, caption: str) -> str:
    """``"Figure 3 — <caption>"``; degrades to just the number, or just the caption, or ``""``."""
    label = figure_label(number)
    text = (caption or "").strip()
    if label and text:
        return label + FIGURE_SEPARATOR + text
    return label or text


def figure_anchor(number: int | None) -> str:
    """The stable in-document id a cross-reference targets (``fig-3``); ``""`` when un-numbered."""
    return "" if number is None else f"fig-{number}"


def number_figures(
    groups: list[GroupCtx], diagrams: list[DiagramCtx], artifacts: list[ArtifactCtx]
) -> int:
    """Stamp a continuous, report-wide ``figure_number`` (1-based) on every figure, in DOCUMENT order.
    Returns the count assigned.

    Document order is: each finding's evidence in board order (each nested child's artifacts, then the
    parent's own gallery -- see the comment in the loop for why that way round) -> attack-path diagrams
    -> the engagement-level evidence appendix. That is the order the ``default`` AND ``compliance`` HTML
    templates render (``reporting/layouts.py``: ``findings`` -> ``diagrams`` -> ``evidence``) and the
    order ``render_docx.render_report_docx`` appends its post-render sections in, so the two
    deliverables number the same figure the same way *structurally* — not by two renderers separately
    remembering to.

    EVERY gallery artifact is numbered, embeddable or not. Whether an artifact's bytes actually make it
    into a given deliverable depends on that renderer's inlining budget and on whether the caller
    supplied an artifact reader at all, so numbering off embed success would hand the same report
    different figure numbers in HTML and DOCX — precisely the defect ext#117 is about."""
    n = 0

    def _stamp(items: list[ArtifactCtx] | list[DiagramCtx]) -> None:
        nonlocal n
        for item in items:
            n += 1
            item.figure_number = n

    for group in groups:
        for finding in group.findings:
            # CHILDREN FIRST, deliberately. The .docx template emits ``{{r f.body }}`` -- which carries
            # the "Affected Hosts" list and the children's evidence -- BEFORE the parent's own evidence
            # loop (``report_templates/build_default_docx.py``), and that order is baked into a binary
            # template. Numbering the parent first made Word print "Figure 3" above "Figure 1".
            # ``render_html._render_finding`` renders children before the gallery to match, so both
            # documents count upward as the reader scrolls.
            for child in finding.children:
                _stamp(child.artifacts)
            _stamp(finding.artifacts)
    _stamp(diagrams)
    _stamp(artifacts)
    return n


def _finding_ctx(finding, *, artifact_url) -> FindingCtx:
    engagement = finding.engagement
    # Resolve this finding's OWN report-variable overlay (``EngagementFinding.variables``, filled by
    # promote from the host's neutral facts — CONTRACT.md §5.4) into the render context so a vuln-DB
    # write-up's ``{{AFFECTED}}``/``{{DOMAIN}}``/etc. resolve to real values instead of surviving
    # verbatim. ``_KeepUndefined`` still applies to any token this overlay doesn't cover — a genuinely
    # unknown ``{{TOKEN}}`` is left untouched, not blanked.
    variables = dict(finding.variables or {})
    session = object_session(finding)
    ctx = (
        build_full_context(session, engagement, finding, extra=variables)
        if session is not None
        else build_context(engagement, finding, extra=variables)
    )
    resolve_var = make_var_resolver(ctx)
    blocks_html = {}
    from scribble.templating import resolve_doc  # local import avoids cycle at module import

    for block, doc in (finding.content_json or {}).items():
        resolved = resolve_doc(doc, ctx)
        blocks_html[block] = render_html.render_block(
            resolved, resolve_var=resolve_var, artifact_url=artifact_url
        )
    artifacts = _artifact_ctxs(finding.artifacts, engagement_id=engagement.id)
    return FindingCtx(
        id=finding.id,
        title=finding.title,
        severity=finding.severity.value,
        cvss_score=finding.cvss_score,
        cvss_vector=finding.cvss_vector,
        target_host=finding.target_host,
        target_port=finding.target_port,
        target_url=finding.target_url,
        blocks_html=blocks_html,
        artifacts=artifacts,
        variables=variables,
        facts_line=_facts_line(variables),
    )


def _nest_findings(ordered_findings, *, artifact_url) -> list[FindingCtx]:
    """Build ``FindingCtx``\\s for one already-ordered/filtered finding list, nesting children
    (``parent_id`` set) under their parent's ``.children`` instead of leaving them top-level.

    A finding is treated as a child only when its ``parent_id`` resolves to *another* finding in this
    same list that is itself a true parent (``parent_id is None``) -- one level of nesting, matching
    the model (no ORM relationship, no extra query: this is a single pass over ``ordered_findings``).
    A finding whose ``parent_id`` doesn't resolve within this list (missing / excluded / cross-group
    parent) falls back to rendering top-level, exactly as it would have before nesting existed.

    That rule lives in ``findings_service.nested_child_ids`` rather than here, because the machine API's
    board listing has to answer "how many findings does the report actually show?" with the SAME rule --
    it counted a promoted parent's children as three top-level findings when the report renders one.
    """
    nested = findings_service.nested_child_ids(ordered_findings)
    children_by_parent: dict[int, list] = {}
    top_level: list = []
    for f in ordered_findings:
        if f.id in nested:
            children_by_parent.setdefault(f.parent_id, []).append(f)
        else:
            top_level.append(f)

    result: list[FindingCtx] = []
    for f in top_level:
        ctx = _finding_ctx(f, artifact_url=artifact_url)
        kids = children_by_parent.get(f.id, [])
        if kids:
            ctx.children = [_finding_ctx(c, artifact_url=artifact_url) for c in kids]
        result.append(ctx)
    return result


def _build_narrative(company_name: str, rollup: SeverityRollup, groups: list[GroupCtx]) -> str:
    """A short, factual executive-summary paragraph synthesized from ``rollup`` (severity counts) and
    the titles of the worst top-level findings -- ADDS to (never replaces) the risk banner / KPI tiles
    ``_render_summary``/``_build_context`` already render."""
    company = company_name or "the target environment"
    if rollup.total == 0:
        return f"This assessment of {company} did not identify any findings within the tested scope."

    crit = rollup.counts.get("critical", 0)
    high = rollup.counts.get("high", 0)
    plural = "finding" if rollup.total == 1 else "findings"
    lead = f"This assessment of {company} identified {rollup.total} {plural} across the environment"

    severity_bits = []
    if crit:
        severity_bits.append(f"{crit} critical")
    if high:
        severity_bits.append(f"{high} high-risk")
    if severity_bits:
        issue_word = "issue" if (crit + high) == 1 else "issues"
        lead += f", including {' and '.join(severity_bits)} {issue_word}."
    else:
        lead += "."

    top_titles: list[str] = []
    seen: set[str] = set()
    for group in groups:
        for finding in group.findings:
            if finding.severity in ("critical", "high") and finding.title not in seen:
                seen.add(finding.title)
                top_titles.append(finding.title)
    top_titles = top_titles[:3]

    if top_titles:
        lead += " The most significant exposures were " + "; ".join(top_titles) + "."
    else:
        lead += (
            " No critical or high-risk issues were identified; findings were limited to "
            "lower-severity observations."
        )
    return lead


def _build_checklists(engagement) -> list[ChecklistCtx]:
    """Build report contexts for the engagement's assigned checklists that opt into the report
    (``include_in_report``). Non-blocking reminders that stay internal are simply absent here."""
    from scribble import checklists as _cl

    finding_titles = {f.id: f.title for f in engagement.findings}
    out: list[ChecklistCtx] = []
    for ec in sorted(engagement.checklists, key=lambda c: c.order_index):
        if not ec.include_in_report:
            continue
        items: list[ChecklistItemCtx] = []
        for it in sorted(ec.items, key=lambda i: i.order_index):
            bucket = _cl.status_bucket(it.status)
            items.append(
                ChecklistItemCtx(
                    section=it.section,
                    text=it.text,
                    guidance=it.guidance,
                    framework=it.framework,
                    control_ref=it.control_ref,
                    status=it.status,
                    bucket=bucket,
                    bucket_label=_cl.BUCKET_LABEL.get(bucket, bucket),
                    note=it.note,
                    finding_id=it.finding_id,
                    finding_title=finding_titles.get(it.finding_id),
                )
            )
        out.append(
            ChecklistCtx(
                id=ec.id,
                name=ec.name,
                kind=ec.kind.value if hasattr(ec.kind, "value") else str(ec.kind),
                rollup=_cl.rollup(ec.items),
                total=len(ec.items),
                items=items,
            )
        )
    return out


def _build_activity_log(engagement) -> list[ActivityEntry]:
    """Chronological engagement activity trail from scribble's OWN timestamps (TimestampMixin) — no
    cross-seam sourcing. Rows: engagement creation, each report-included finding added, each evidence
    upload, each attack-path diagram. Oldest-first; a row with no ``created_at`` sorts last. Filtered by
    ``include_in_report`` so the appendix never leaks an excluded draft into the deliverable."""
    def _fmt(dt) -> str:
        return dt.strftime("%Y-%m-%d %H:%M UTC") if dt is not None else ""

    events: list[tuple[object, ActivityEntry]] = []
    created = getattr(engagement, "created_at", None)
    if created is not None:
        events.append((created, ActivityEntry(_fmt(created), "engagement",
                                               f"Engagement created: {engagement.name}")))
    for f in engagement.findings:
        if not getattr(f, "include_in_report", True):
            continue
        events.append((f.created_at, ActivityEntry(_fmt(f.created_at), "finding",
                                                    f"Finding added: {f.title}")))
    for a in engagement.artifacts:
        if not getattr(a, "include_in_report", True):
            continue
        events.append((a.created_at, ActivityEntry(_fmt(a.created_at), "evidence",
                                                    f"Evidence uploaded: {a.filename}")))
    for d in engagement.diagrams:
        if not getattr(d, "include_in_report", True):
            continue
        label = d.caption or "attack path"
        events.append((d.created_at, ActivityEntry(_fmt(d.created_at), "diagram",
                                                    f"Diagram added: {label}")))
    # Oldest-first; None created_at sorts last. The leading bool keeps None out of a datetime comparison
    # (tuples stop at the first differing element, so a present vs None row never compares the datetimes).
    events.sort(key=lambda e: (e[0] is None, e[0]))
    return [entry for _, entry in events]


def build_report_context(engagement, *, artifact_url=None) -> ReportContext:
    """Assemble a ``ReportContext`` from a loaded ``Engagement`` (with relationships available).

    ``artifact_url(artifact_id) -> str`` supplies inline-image src; None yields empty srcs (fine for
    tests / structure checks).
    """
    artifact_url = artifact_url or (lambda _id: "")

    groups_out: list[GroupCtx] = []
    counts: dict[Severity, int] = {}

    def _tally(findings):
        for f in findings:
            counts[f.severity] = counts.get(f.severity, 0) + 1

    for group in sorted(engagement.groups, key=lambda g: g.order_index):
        if not group.include_in_report:
            continue
        ordered = _order_findings(group.findings, group.order_mode)
        _tally(ordered)
        at = group.assessment_type
        groups_out.append(
            GroupCtx(
                id=group.id,
                name=group.name,
                type_slug=at.slug if at else None,
                color=at.color if at else None,
                findings=_nest_findings(ordered, artifact_url=artifact_url),
            )
        )

    ungrouped = [f for f in engagement.findings if f.group_id is None and f.include_in_report]
    if ungrouped:
        ordered = _order_findings(ungrouped, OrderMode.auto_severity)
        _tally(ordered)
        groups_out.append(
            GroupCtx(
                id=None,
                name="Ungrouped",
                type_slug=None,
                color=None,
                findings=_nest_findings(ordered, artifact_url=artifact_url),
            )
        )

    rollup = SeverityRollup(
        counts={s.value: counts.get(s, 0) for s in Severity},
        total=sum(counts.values()),
        overall=risk_rating(counts).value,
    )
    # ``Engagement.client`` no longer exists (soft reference, no static relationship -- see
    # docs/LOTEK_ADOPTION.md §3.1 / scribble.models.Engagement.resolve_client). Resolve through the
    # SAME session the engagement is already attached to (every caller builds/loads it inside an open
    # ``with open_session() as db`` block) rather than widening this frozen-contract function's
    # signature to take a session explicitly.
    session = object_session(engagement)
    client = engagement.resolve_client(session) if session is not None else None
    company_name = engagement.company_name or (client.name if client else "")
    # Engagement-level evidence: attached to the engagement, NOT to any finding (``finding_id`` null).
    # These have no finding gallery to appear in, so without this list they reached no deliverable at
    # all (ext#40). A finding's own artifacts stay where they were — in that finding's gallery.
    engagement_artifacts = _artifact_ctxs(
        [a for a in engagement.artifacts if a.finding_id is None], engagement_id=engagement.id
    )
    diagrams = _diagram_ctxs(engagement.diagrams)
    # ext#117: assigned HERE, once, so both renderers read the same numbers off the same context.
    number_figures(groups_out, diagrams, engagement_artifacts)
    return ReportContext(
        engagement_id=engagement.id,
        engagement_name=engagement.name,
        company_name=company_name,
        client_name=client.name if client else None,
        scope_type=engagement.scope_type,
        start_date=engagement.start_date.isoformat() if engagement.start_date else None,
        end_date=engagement.end_date.isoformat() if engagement.end_date else None,
        groups=groups_out,
        rollup=rollup,
        artifacts=engagement_artifacts,
        diagrams=diagrams,
        checklists=_build_checklists(engagement),
        variables=build_context(engagement),
        narrative=_build_narrative(company_name, rollup, groups_out),
        activity_log=_build_activity_log(engagement),
    )
