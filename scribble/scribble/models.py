"""Scribble data model (SQLAlchemy 2.0 declarative).

See PLAN.md §4. Field shapes align with Lotek's ``Client`` / ``Finding`` / ``Asset`` / ``Tag`` so the
package can mount into Lotek and reconcile at the port checkpoint. This module is a FROZEN CONTRACT for
Sprint 0 — feature workstreams build against these tables; schema changes go through the driver.

Content-bearing columns (``content_json``) hold ProseMirror/TipTap JSON keyed by block name
(see ``scribble.content.schema``); ``content_html`` caches the sanitized rendered HTML.
"""

from __future__ import annotations

import uuid
from datetime import date

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from scribble.db import Base, ScribbleUuid, SoftHostId, TimestampMixin
from scribble.enums import (
    ArtifactKind,
    ArtifactPlacement,
    ChecklistKind,
    Confidence,
    FindingStatus,
    OrderMode,
    ReportFormat,
    Severity,
    VariableScope,
    VariableType,
)

# --------------------------------------------------------------------------- clients & engagements


class Client(Base, TimestampMixin):
    """A customer org. Aligns with Lotek's minimal ``Client`` (id, name).

    Used as the DEFAULT client model standalone (see ``scribble.deps.client_model``); when mounted with
    a host ``client_model`` injected (e.g. Lotek's own ``Client``), new engagements are created against
    the host's table instead and this table stays empty (docs/LOTEK_ADOPTION.md §3.1). No ``engagements``
    back-reference: ``Engagement.client_id`` is a soft reference resolved at read time, not a static FK
    (see ``Engagement.resolve_client``), because a fixed relationship can't join to "whichever client
    model is mounted right now".
    """

    __tablename__ = "scribble_clients"

    id: Mapped[uuid.UUID] = mapped_column(ScribbleUuid, primary_key=True, default=uuid.uuid7)
    name: Mapped[str] = mapped_column(String(255), unique=True)


class Engagement(Base, TimestampMixin):
    """One assessment container. Many per client, concurrent (incl. same client)."""

    __tablename__ = "scribble_engagements"

    id: Mapped[uuid.UUID] = mapped_column(ScribbleUuid, primary_key=True, default=uuid.uuid7)
    # Soft reference, NOT a foreign key (docs/LOTEK_ADOPTION.md §3.1): may point at ``scribble_clients``
    # (standalone) or the host's own client table (mounted, e.g. Lotek's ``clients``) -- a static FK/
    # relationship can only target one table, so resolution goes through ``scribble.deps.client_model``
    # at read/write time instead (see ``resolve_client`` below). Same pattern as
    # ``EngagementFinding.asset_id`` below.
    #
    # Schema-history note: ``scribble.db.create_all`` is additive-only (``Base.metadata.create_all``) --
    # it does not retrofit a table that already exists on disk. A ``scribble_engagements`` table created
    # by a PRE-existing checkout (which had ``client_id`` as a real FK to ``scribble_clients.id``) keeps
    # that FK constraint physically in its SQLite schema until the table is rebuilt; only a freshly
    # created database picks up the new (FK-less) column. There is no migration framework in this repo
    # (no alembic, no ``scribble/migrations.py``) to retrofit an existing file automatically -- drop/
    # recreate the local dev DB (e.g. ``instance/scribble.db``) if you hit a stale constraint.
    #
    # ``SoftHostId`` (scribble.db), not a plain ``Integer``: the host id this points at may be a sequential
    # int (standalone/legacy) or a ``uuid.UUID`` (Lotek v2's UUIDv7 PKs) -- see SoftHostId's docstring for
    # the round-trip + Postgres-retrofit caveat.
    client_id: Mapped[int | uuid.UUID | None] = mapped_column(SoftHostId)
    name: Mapped[str] = mapped_column(String(255))
    scope_type: Mapped[str] = mapped_column(String(64), default="external")
    company_name: Mapped[str | None] = mapped_column(String(255))  # {{COMPANY_NAME}} default
    start_date: Mapped[date | None] = mapped_column(Date)
    end_date: Mapped[date | None] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(32), default="in_progress")
    guid: Mapped[str | None] = mapped_column(String(64), unique=True)
    # Soft ref (#49) to the CORE engagement id core handed back from ``POST /api/v1/engagements``
    # (a UUIDv7 in Lotek v2, a sequential int on older/standalone hosts) -- lets a PAT caller that only
    # holds the core id address this scribble engagement without first discovering its own integer PK.
    # No FK (same soft-ref pattern as ``client_id``/``owner_id`` above): the host may not even be Lotek.
    # ``index=True``, NOT ``unique=True`` -- ``scribble.db.create_all``'s additive path can retrofit an
    # index on a pre-existing table but cannot retrofit a UNIQUE constraint; duplicates are prevented
    # only by the single-writer assumption at create time (see ``_resolve_engagement`` in api_pat.py).
    core_engagement_id: Mapped[int | uuid.UUID | None] = mapped_column(SoftHostId, nullable=True, index=True)
    distribution_list: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[str | None] = mapped_column(String(128))
    # Ownership attribution: the host user id who created this engagement (SOFT ref to the host's users
    # table — no FK; None standalone). Tracked for admin oversight/attribution ONLY — engagements stay
    # team-shared (every operator can view/edit/collaborate), so this is NOT an access gate. See
    # docs/LOTEK_ADOPTION.md §4. ``SoftHostId``, not ``Integer`` -- same int-or-UUID host id shape as
    # ``client_id`` above.
    owner_id: Mapped[int | uuid.UUID | None] = mapped_column(SoftHostId, nullable=True, index=True)

    groups: Mapped[list[FindingGroup]] = relationship(
        back_populates="engagement", cascade="all, delete-orphan", order_by="FindingGroup.order_index"
    )
    findings: Mapped[list[EngagementFinding]] = relationship(
        back_populates="engagement", cascade="all, delete-orphan"
    )
    artifacts: Mapped[list[Artifact]] = relationship(
        back_populates="engagement", cascade="all, delete-orphan"
    )
    variable_values: Mapped[list[VariableValue]] = relationship(
        back_populates="engagement", cascade="all, delete-orphan"
    )
    checklists: Mapped[list[EngagementChecklist]] = relationship(
        back_populates="engagement", cascade="all, delete-orphan"
    )
    diagrams: Mapped[list[EngagementDiagram]] = relationship(
        back_populates="engagement",
        cascade="all, delete-orphan",
        order_by="EngagementDiagram.order_index",
    )

    def resolve_client(self, session):
        """Load this engagement's client through the currently-mounted client model, or ``None``.

        Replaces the old ``Engagement.client`` relationship, which required a static FK to a single
        table. ``client_id`` is a soft reference that may point at ``scribble_clients`` (standalone) or
        the host's client table (mounted) -- which one is live is only known via
        ``scribble.deps.client_model()`` at call time, so this does a plain ``session.get`` against
        whatever that resolves to instead of a relationship traversal.
        """
        if self.client_id is None:
            return None
        from scribble.deps import client_model  # local import: avoids a models.py <-> deps.py cycle

        return session.get(client_model(), self.client_id)


# --------------------------------------------------------------------------- grouping (report sections)


class AssessmentType(Base, TimestampMixin):
    """User-managed lookup (Internal / External / Web App / Device-Mobile / your own). NOT hardcoded."""

    __tablename__ = "scribble_assessment_types"

    id: Mapped[uuid.UUID] = mapped_column(ScribbleUuid, primary_key=True, default=uuid.uuid7)
    name: Mapped[str] = mapped_column(String(128), unique=True)
    slug: Mapped[str] = mapped_column(String(128), unique=True)
    color: Mapped[str | None] = mapped_column(String(16))
    default_order: Mapped[int] = mapped_column(Integer, default=0)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class FindingGroup(Base, TimestampMixin):
    """A report section within an engagement (one per assessment type present). Drag-orderable;
    carries its child findings when reordered."""

    __tablename__ = "scribble_finding_groups"

    id: Mapped[uuid.UUID] = mapped_column(ScribbleUuid, primary_key=True, default=uuid.uuid7)
    engagement_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("scribble_engagements.id"))
    assessment_type_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("scribble_assessment_types.id"))
    name: Mapped[str] = mapped_column(String(128))
    order_index: Mapped[int] = mapped_column(Integer, default=0)
    order_mode: Mapped[OrderMode] = mapped_column(Enum(OrderMode), default=OrderMode.auto_severity)
    include_in_report: Mapped[bool] = mapped_column(Boolean, default=True)

    engagement: Mapped[Engagement] = relationship(back_populates="groups")
    assessment_type: Mapped[AssessmentType | None] = relationship()
    findings: Mapped[list[EngagementFinding]] = relationship(
        back_populates="group", order_by="EngagementFinding.order_index"
    )


# --------------------------------------------------------------------------- vulnerability library


class VulnerabilityTemplate(Base, TimestampMixin):
    """Reusable finding write-up (FACTION ``DefaultVulnerability``). Content carries {{VARIABLES}}."""

    __tablename__ = "scribble_vuln_templates"

    id: Mapped[uuid.UUID] = mapped_column(ScribbleUuid, primary_key=True, default=uuid.uuid7)
    name: Mapped[str] = mapped_column(String(512))
    category: Mapped[str | None] = mapped_column(String(255))
    default_severity: Mapped[Severity] = mapped_column(Enum(Severity), default=Severity.medium)
    cvss_score: Mapped[float | None] = mapped_column(Float)
    cvss_vector: Mapped[str | None] = mapped_column(String(255))
    content_json: Mapped[dict] = mapped_column(JSON, default=dict)  # {block_name: prosemirror_doc}
    content_html: Mapped[dict] = mapped_column(JSON, default=dict)  # {block_name: cached_html}
    references: Mapped[list] = mapped_column(JSON, default=list)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    # True when the row was authored over the PAT/machine API rather than by a human in the library UI.
    #
    # The template library is a SINGLE, SHARED, tenant-free table, and `EngagementFinding.from_template`
    # copies `content_json` VERBATIM into a client-facing finding. `promote.py` instantiates templates
    # AUTOMATICALLY whenever a `ScribbleVulnMap` rule matches — no human chooses that. Since a
    # write-scoped PAT can already install a global vuln-map rule, letting an agent also author the
    # CONTENT would mean agent-written prose could land in ANOTHER tenant's deliverable with nobody ever
    # reading it. That is the outward-effect class this platform keeps human-gated (INV-EXT-02).
    #
    # So machine-authored templates are excluded from AUTOMATIC resolution (`resolve_vuln_template`),
    # while remaining explicitly instantiable by id — an act performed by a caller who already holds
    # access to the destination engagement, against a template they named. Authoring stays available to
    # agents; silent cross-tenant adoption does not.
    machine_authored: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    tags: Mapped[list[Tag]] = relationship(secondary="scribble_template_tags")


class ScribbleVulnMap(Base, TimestampMixin):
    """Maps a promoted lotek scan finding's (source, title, dedupe_key) signature to a library
    ``VulnerabilityTemplate`` so the promote step auto-selects the right write-up instead of bridging
    the raw finding verbatim.

    Formerly lotek core's ``VulnMap`` (``vuln_template_map``, a soft/no-FK reference because Scribble
    was an optional extension that might not be mounted). Now that promotion is entirely Scribble's own
    concern -- the host only ever hands over neutral ``FindingDTO``s (see ``src/app/host_contract.py``)
    -- this table lives here instead, with a REAL foreign key: Scribble always has its own
    ``scribble_vuln_templates`` table by definition. Resolution is most-specific-first: ``dedupe_prefix``
    (prefix of the lotek finding's ``dedupe_key``) beats ``source`` + ``title_pattern`` (glob on the
    finding's ``title``) beats ``source`` alone -- see ``scribble/promote.py::resolve_vuln_template``.
    """

    __tablename__ = "scribble_vuln_map"

    id: Mapped[uuid.UUID] = mapped_column(ScribbleUuid, primary_key=True, default=uuid.uuid7)
    source: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    title_pattern: Mapped[str | None] = mapped_column(String(255), nullable=True)
    dedupe_prefix: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    template_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("scribble_vuln_templates.id"), nullable=False)
    created_by: Mapped[str | None] = mapped_column(String(80), nullable=True)


class EngagementFinding(Base, TimestampMixin):
    """A finding instance in an engagement (FACTION ``Vulnerability``). Editable copy of a template;
    keeps a nullable link back to the template it came from. Field names mirror Lotek's ``Finding``."""

    __tablename__ = "scribble_findings"

    id: Mapped[uuid.UUID] = mapped_column(ScribbleUuid, primary_key=True, default=uuid.uuid7)
    engagement_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("scribble_engagements.id"))
    group_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("scribble_finding_groups.id"))
    template_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("scribble_vuln_templates.id"))
    # Soft reference (NOT a foreign key -- mirrors ``asset_id`` below) to a host Lotek ``Finding.id``,
    # set by ``from_lotek_finding`` when promoting a scan finding. Lets the promote flow dedup (has this
    # Lotek finding already been promoted into this engagement?) without a cross-table FK, since a
    # standalone Scribble has no Lotek ``Finding`` table to reference at all.
    # ``SoftHostId``, NOT ``Integer``: a host ``Finding.id`` is a sequential int on a legacy/standalone
    # host but a ``uuid.UUID`` on Lotek v2 (UUIDv7 PKs). Declared ``Integer``, promoting a v2 finding
    # raises `(psycopg.errors.CannotCoerce) cannot cast type uuid to integer` on INSERT — the exact
    # red-path INV-INTEGRITY-03 describes. It went unnoticed because SQLite accepts the value silently;
    # only real Postgres refuses it. Any column holding a CORE id must use SoftHostId (see its docstring).
    source_finding_id: Mapped[int | uuid.UUID | None] = mapped_column(
        SoftHostId, nullable=True, index=True
    )
    # Self-referential nesting: a PARENT finding groups instances of the same vuln TYPE (the resolved
    # vuln-DB template, else a source/title signature) as CHILDREN. Set by promotion aggregation;
    # adjustable by drag-to-nest on the board. Children are queried WHERE parent_id = id (no ORM
    # relationship, to keep the self-ref config trivial and robust).
    parent_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("scribble_findings.id"), nullable=True)

    title: Mapped[str] = mapped_column(String(512))
    category: Mapped[str | None] = mapped_column(String(255))
    severity: Mapped[Severity] = mapped_column(Enum(Severity), default=Severity.medium)
    confidence: Mapped[Confidence] = mapped_column(Enum(Confidence), default=Confidence.medium)
    cvss_score: Mapped[float | None] = mapped_column(Float)
    cvss_vector: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[FindingStatus] = mapped_column(Enum(FindingStatus), default=FindingStatus.new)

    content_json: Mapped[dict] = mapped_column(JSON, default=dict)
    content_html: Mapped[dict] = mapped_column(JSON, default=dict)

    order_index: Mapped[int] = mapped_column(Integer, default=0)  # within its group
    include_in_report: Mapped[bool] = mapped_column(Boolean, default=True)

    # Per-finding target context that feeds {{TARGET_*}} variables.
    target_host: Mapped[str | None] = mapped_column(String(255))
    target_port: Mapped[str | None] = mapped_column(String(16))
    target_url: Mapped[str | None] = mapped_column(String(1024))
    # Optional link to a host ``Asset`` when mounted — ``SoftHostId`` for the SAME reason as
    # ``source_finding_id`` above: a v2 host Asset id is a UUID, and an Integer column cannot hold it.
    asset_id: Mapped[int | uuid.UUID | None] = mapped_column(SoftHostId, nullable=True)

    analyst_notes: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[str | None] = mapped_column(String(128))

    # Report-variable overlay for THIS finding: {"AFFECTED": "…", "DOMAIN": "…", …}. Filled by the
    # promote step from the host's FindingDTO.facts (scribble/facts.py) and by deterministic group-fact
    # synthesis for a PARENT; applied at render as build_full_context(extra=…) so {{TOKENS}} in the
    # vuln-DB write-up resolve. The token VOCABULARY lives here, in the reporter — the host only ever
    # emits neutral facts. Additive column (``scribble.db.create_all`` retrofits it with ``ALTER TABLE
    # ADD COLUMN`` on an existing DB), so a row created before this column existed reads NULL -- every
    # read site uses ``finding.variables or {}``, never this column bare.
    variables: Mapped[dict] = mapped_column(JSON, default=dict, nullable=True)

    engagement: Mapped[Engagement] = relationship(back_populates="findings")
    group: Mapped[FindingGroup | None] = relationship(back_populates="findings")
    template: Mapped[VulnerabilityTemplate | None] = relationship()
    artifacts: Mapped[list[Artifact]] = relationship(
        back_populates="finding", order_by="Artifact.order_index"
    )
    tags: Mapped[list[Tag]] = relationship(secondary="scribble_finding_tags")

    @classmethod
    def from_template(cls, template: VulnerabilityTemplate, **overrides) -> EngagementFinding:
        """Instantiate a finding from a library template (copies content; keeps the link)."""
        data = dict(
            template_id=template.id,
            title=template.name,
            category=template.category,
            severity=template.default_severity,
            cvss_score=template.cvss_score,
            cvss_vector=template.cvss_vector,
            content_json=dict(template.content_json or {}),
            content_html=dict(template.content_html or {}),
        )
        data.update(overrides)
        return cls(**data)

    @classmethod
    def from_lotek_finding(cls, finding, **overrides) -> EngagementFinding:
        """Adapter: promote a host scan finding (a ``host_contract.FindingDTO``, or anything
        duck-shaped like one) into an engagement finding. Bridges the two models without overloading
        the host's tool-derived table (see PLAN.md §4, §16).

        Constructs the severity via ``scribble.deps.severity_enum()`` so a mounted host's own
        ``Severity`` is used when injected (docs/LOTEK_ADOPTION.md §3.2) -- falls back to Scribble's own
        ``scribble.enums.Severity`` standalone; the two vocabularies are value-identical, so this is
        purely "one severity object when mounted", never a change in which strings are accepted.

        Severity normalization (CONTRACT C11): ``FindingDTO.severity`` is a plain ``str`` (e.g.
        ``"high"``), NOT an enum -- ``getattr(getattr(finding, "severity", None), "value", "medium")``
        would silently downgrade EVERY promoted severity to ``"medium"`` because a plain string has no
        ``.value`` attribute. Read the raw attribute first, then unwrap ``.value`` ONLY if present (still
        accepts an enum-shaped ``severity``, e.g. the old lotek ORM ``Finding.severity``, for callers that
        haven't moved to the DTO), falling back to the raw value itself.
        """
        from scribble.content.schema import doc_from_text
        from scribble.deps import severity_enum  # local import: avoids a models.py <-> deps.py cycle

        SeverityEnum = severity_enum()
        raw_severity = getattr(finding, "severity", None)
        severity_value = getattr(raw_severity, "value", raw_severity) or "medium"
        # Map the lotek finding's prose into content blocks so a promoted, un-templated finding renders
        # REAL content instead of "No content." (content_json was left at its empty default). A
        # VulnMap-resolved template still wins via from_template when one matches. Raw evidence becomes a
        # 'details' block ONLY as a last resort (no prose) and NEVER a raw JSON record (the config
        # parsers store evidence as json.dumps(record) -- a blob that just duplicates the description).
        content_json: dict = {}
        if getattr(finding, "description", None):
            content_json["description"] = doc_from_text(str(finding.description))
        if getattr(finding, "remediation", None):
            content_json["remediation"] = doc_from_text(str(finding.remediation))
        # ``FindingDTO.evidence`` (was ``Finding.evidence_refs`` on the old ORM row the host used to hand
        # over directly -- CONTRACT C11).
        ev = getattr(finding, "evidence", None)
        if ev and not content_json:
            ev = str(ev).strip()
            if ev and ev[0] not in "{[":
                content_json["details"] = doc_from_text(ev)
        data = dict(
            title=getattr(finding, "title", "Untitled"),
            category=getattr(finding, "category", None),
            severity=SeverityEnum(severity_value),
            cvss_score=getattr(finding, "cvss_score", None),
            content_json=content_json,
            analyst_notes=getattr(finding, "analyst_notes", None),
            source_finding_id=getattr(finding, "id", None),
        )
        data.update(overrides)
        return cls(**data)


# --------------------------------------------------------------------------- artifacts (evidence)


class Artifact(Base, TimestampMixin):
    """Evidence: screenshot / text / arbitrary file. The include/exclude + caption + order system
    FACTION never had. Bytes live on disk (storage_path); rows are metadata."""

    __tablename__ = "scribble_artifacts"

    id: Mapped[uuid.UUID] = mapped_column(ScribbleUuid, primary_key=True, default=uuid.uuid7)
    engagement_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("scribble_engagements.id"))
    finding_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("scribble_findings.id"))

    kind: Mapped[ArtifactKind] = mapped_column(Enum(ArtifactKind), default=ArtifactKind.screenshot)
    placement: Mapped[ArtifactPlacement] = mapped_column(
        Enum(ArtifactPlacement), default=ArtifactPlacement.attached
    )
    filename: Mapped[str] = mapped_column(String(512))
    content_type: Mapped[str | None] = mapped_column(String(128))
    # WHERE THE BYTES ARE — one column, one answer, and deliberately NOT two.
    #
    # Either an `obj:<uuid>` reference into the CORE object store (SeaweedFS, reached through the host
    # contract) or a path relative to `artifact_root`. A first cut carried a second `object_id` column
    # beside this one, and then every reader had to know which of the two was authoritative for a
    # given row — five call sites free to disagree, and two of them did. A pre-cutover row's existing
    # value is already a valid reference of the disk kind, so one column also means no backfill and no
    # migration.
    #
    # `artifacts_storage.object_id_of()` is the only thing that parses it. Scribble never sees the
    # bucket or the S3 key — `HostObjects` returns a ref without one, which keeps the store
    # dashboard-only.
    storage_path: Mapped[str] = mapped_column(String(1024))
    byte_size: Mapped[int | None] = mapped_column(Integer)
    sha256: Mapped[str | None] = mapped_column(String(64))
    caption: Mapped[str | None] = mapped_column(Text)
    order_index: Mapped[int] = mapped_column(Integer, default=0)
    include_in_report: Mapped[bool] = mapped_column(Boolean, default=True)
    created_by: Mapped[str | None] = mapped_column(String(128))
    # Client-supplied dedup token for retried uploads (the offline upload-outbox story, PLAN.md §19).
    # Nullable (additive-migration-safe) and unindexed-unique-on-purpose: uniqueness is enforced by a
    # query-based lookup on (engagement_id, idempotency_key) in artifacts_api.create_artifact, not a DB
    # constraint, because the additive migration in scribble/db.py can only retrofit plain indexes onto
    # an existing table, never a composite UNIQUE constraint.
    #
    # TEXT, not a bounded VARCHAR: the client derives this key from the engagement id, finding id and file
    # basename (lotek's attach-evidence.sh emits `ev-<eng>-<finding>-<name>-<sha>`). Once #372 migrated
    # engagement/finding PKs to 36-char UUIDs a routine key runs ~120 chars, overflowing the old
    # VARCHAR(80) — Postgres raises StringDataRightTruncation (a 500 on every evidence upload) while SQLite
    # silently stored it. The basename alone is a String(512), so no bounded length is safe here.
    idempotency_key: Mapped[str | None] = mapped_column(Text, nullable=True, index=True)

    engagement: Mapped[Engagement] = relationship(back_populates="artifacts")
    finding: Mapped[EngagementFinding | None] = relationship(back_populates="artifacts")


# --------------------------------------------------------------------------- attack-path diagrams


class EngagementDiagram(Base, TimestampMixin):
    """A linked vector attack-path diagram, embedded in the report as a self-contained HTML SNAPSHOT
    (ext#48). Scribble is a separate extension from vector and has no seam to reach it at render time
    (no host hook exposes vector), so this table stores vector's already-self-contained
    ``export.html`` verbatim rather than fetching it live — a caller GETs
    ``/vector/machine/diagrams/<id>/export.html`` and POSTs the result here (see
    ``scribble_link_attack_path`` in ``api_pat.py``). ``diagram_ref`` is a soft reference (the vector
    diagram's UUID, stored as text) kept for provenance/dedup only; nothing here re-fetches through it.

    ``engagement_id`` is a real FK to ``scribble_engagements.id`` — UUIDv7 since ext#36/lotek#335
    migrated every Scribble PK off sequential integers. This table was authored on the int-PK branch
    and rebased onto the UUID one, exactly the "trivial rebase" the earlier note here predicted.
    """

    __tablename__ = "scribble_engagement_diagram"

    id: Mapped[uuid.UUID] = mapped_column(ScribbleUuid, primary_key=True, default=uuid.uuid7)
    engagement_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("scribble_engagements.id"), index=True)
    diagram_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)
    caption: Mapped[str | None] = mapped_column(String(255), nullable=True)
    embed_html: Mapped[str | None] = mapped_column(Text, nullable=True)
    order_index: Mapped[int] = mapped_column(Integer, default=0)
    include_in_report: Mapped[bool] = mapped_column(Boolean, default=True)

    engagement: Mapped[Engagement] = relationship(back_populates="diagrams")


# --------------------------------------------------------------------------- template variables


class TemplateVariable(Base, TimestampMixin):
    """A variable definition (FACTION ``CustomType``). builtin=True for the seeded core set."""

    __tablename__ = "scribble_variables"

    id: Mapped[uuid.UUID] = mapped_column(ScribbleUuid, primary_key=True, default=uuid.uuid7)
    key: Mapped[str] = mapped_column(String(128), unique=True)  # referenced as {{key}}
    label: Mapped[str] = mapped_column(String(255))
    scope: Mapped[VariableScope] = mapped_column(Enum(VariableScope), default=VariableScope.engagement)
    value_type: Mapped[VariableType] = mapped_column(Enum(VariableType), default=VariableType.str_)
    default_value: Mapped[str | None] = mapped_column(Text)
    options: Mapped[list] = mapped_column(JSON, default=list)
    builtin: Mapped[bool] = mapped_column(Boolean, default=False)

    # DECLARATIVE mapping from the host's neutral FACTS to this variable's value. An ORDERED list of
    # candidate rules; the FIRST one that resolves non-empty wins. This is DATA, deliberately: the host
    # declares what a TOOL emits (Module.parser_config["facts"]), and this declares what the REPORT does
    # with it -- so a new tool needs no Python on either side of the boundary.
    #
    #   [{"fact": "accounts", "shape": "list"},
    #    {"fact": "accounts_extracted", "shape": "count", "template": "{value} domain accounts"},
    #    {"field": "target_host", "shape": "host"}]
    #
    # ``fact`` = a key of FindingDTO.facts · ``field`` = an allowlisted FindingDTO attribute ·
    # ``shape`` = how to render it AND how to combine children into a parent (scribble/facts.py's ONE
    # rule table) · ``template`` = optional "{value}" wrapper. [] / NULL = never derived from facts (the
    # seeded TARGET_* builtins are computed structurally by resolver.build_context).
    from_facts: Mapped[list] = mapped_column(JSON, default=list, nullable=True)
    # OPTIONAL: also write the resolved value onto this EngagementFinding column, so a token that is
    # ALREADY a structural builtin (TARGET_URL <- resolver.build_context reads finding.target_url) gets
    # populated by the same declaration instead of by promote-time Python. Allowlisted to
    # {"target_host", "target_port", "target_url"}; anything else is ignored.
    target_column: Mapped[str | None] = mapped_column(String(32), nullable=True)


class VariableValue(Base, TimestampMixin):
    """A variable value bound to an engagement or a finding (FACTION ``CustomField``)."""

    __tablename__ = "scribble_variable_values"
    __table_args__ = (UniqueConstraint("variable_id", "engagement_id", "finding_id"),)

    id: Mapped[uuid.UUID] = mapped_column(ScribbleUuid, primary_key=True, default=uuid.uuid7)
    variable_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("scribble_variables.id"))
    engagement_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("scribble_engagements.id"))
    finding_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("scribble_findings.id"))
    value: Mapped[str | None] = mapped_column(Text)

    variable: Mapped[TemplateVariable] = relationship()
    engagement: Mapped[Engagement | None] = relationship(back_populates="variable_values")


# --------------------------------------------------------------------------- tags (aligns w/ Lotek)


class Tag(Base, TimestampMixin):
    __tablename__ = "scribble_tags"

    id: Mapped[uuid.UUID] = mapped_column(ScribbleUuid, primary_key=True, default=uuid.uuid7)
    name: Mapped[str] = mapped_column(String(128), unique=True)
    color: Mapped[str | None] = mapped_column(String(16))


class FindingTag(Base):
    __tablename__ = "scribble_finding_tags"

    finding_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("scribble_findings.id"), primary_key=True)
    tag_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("scribble_tags.id"), primary_key=True)


class TemplateTag(Base):
    __tablename__ = "scribble_template_tags"

    template_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("scribble_vuln_templates.id"), primary_key=True)
    tag_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("scribble_tags.id"), primary_key=True)


# --------------------------------------------------------------------------- report templates & renders


class ReportTemplate(Base, TimestampMixin):
    __tablename__ = "scribble_report_templates"

    id: Mapped[uuid.UUID] = mapped_column(ScribbleUuid, primary_key=True, default=uuid.uuid7)
    name: Mapped[str] = mapped_column(String(255))
    engagement_type: Mapped[str | None] = mapped_column(String(64))
    docx_path: Mapped[str | None] = mapped_column(String(1024))
    html_template_name: Mapped[str | None] = mapped_column(String(255))
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)


class ReportRender(Base, TimestampMixin):
    __tablename__ = "scribble_report_renders"

    id: Mapped[uuid.UUID] = mapped_column(ScribbleUuid, primary_key=True, default=uuid.uuid7)
    engagement_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("scribble_engagements.id"))
    format: Mapped[ReportFormat] = mapped_column(Enum(ReportFormat))
    path: Mapped[str] = mapped_column(String(1024))
    context_hash: Mapped[str | None] = mapped_column(String(64))
    created_by: Mapped[str | None] = mapped_column(String(128))


# --------------------------------------------------------------------------- report themes (override
# provenance + the per-install default) — ext#113/#105.
#
# See ``scribble/CONTEXT.md``'s **Provenance** entry: a Theme is **bundled** (ships inside
# ``reporting/theme_files.py``'s ``report_themes/`` package), **installed** (a separate Python package
# Scribble discovers — not implemented yet, see ``75159ed``'s cut of ``theme_discovery.py``), or
# **override** (data an operator supplied at runtime). ``ScribbleThemeOverride`` is the "override" half:
# every row in this table IS an override Theme, so — unlike ``reporting.marks.PROVENANCES`` — no
# ``provenance`` COLUMN exists here; provenance is a property of which table a Theme's row lives in, not
# a value stored alongside it.
#
# An install-wide default Theme (`ScribbleSettings`) is a SEPARATE, second table rather than a column on
# this one: #105 originally proposed folding both into one row, but a default Theme need not be an
# override at all (the common case is a bundled ``light``/``dark`` default), so it cannot be a column
# on a table whose every row IS an override.


class ScribbleThemeOverride(Base, TimestampMixin):
    """One operator-supplied report Theme (CONTEXT.md Provenance: **override**) — ext#113.

    ``source_toml`` is the operator's ORIGINAL TOML text, stored verbatim — not the parsed
    :class:`~scribble.reporting.theme_files.ThemeFile` dataclass. Two reasons this must be text, not a
    parsed blob: (1) it is the operator's own artifact and has to round-trip byte-for-byte back into an
    edit form, and (2) ``reporting.theme_files._parse_theme_toml`` is already the ONE validator every
    other Theme (bundled, and eventually installed) is checked against — a second, JSON-shaped schema
    for override Themes would be a second grammar to keep in sync with the first, exactly the
    write-path/render-path duplication ``reporting.marks.resolve_mark``'s docstring warns against. Every
    read of an override Theme's payload re-runs ``_parse_theme_toml`` on this column, so a Theme that
    validated at upload time and would now fail (e.g. ``reporting.tokens.ALLOWED_TOKENS`` shrank in a
    later release) is caught at RENDER time too, never silently trusted because it was accepted once.

    ``name`` is both this row's lookup key AND required to equal the stored TOML's own
    ``[identity].name`` — the same rule a bundled Theme's FILENAME is held to (see
    ``theme_files._parse_theme_toml``'s "does not match the filename" check, reused verbatim here since
    an override Theme has no filename to anchor to instead). ``scribble.themes_api`` refuses to store a
    ``name`` that collides with a BUNDLED Theme (``reporting.theme_files.list_theme_files()``) — bundled
    wins, so an operator cannot shadow ``light``/``dark`` with their own data. The column is UNIQUE so two
    override Themes can never collide with EACH OTHER either.

    🔴 Raster-only Marks, and this schema deliberately does NOT stop an operator writing a ``[marks]``
    table into ``source_toml``. ``theme_files._parse_theme_toml`` validates ``[marks]`` as a table and
    reads ``[marks].logo_svg`` as bounded SOURCE TEXT — it does not sanitize it, on purpose, so that
    there is exactly one opinion about what an acceptable Mark is. That opinion is
    ``reporting.marks.resolve_mark``, whose ``_SVG_ALLOWED_PROVENANCES`` is closed over
    ``{"bundled", "installed"}`` ONLY, deliberately excluding ``"override"`` — so an operator-uploaded
    Theme's SVG is refused at render time no matter what this table holds. Storing it unsanitized and
    gating at render is the safer arrangement of the two: it keeps the stored artifact identical to
    what the operator submitted (so it round-trips for editing and can be re-vetted if the gate
    tightens) while making the gate impossible to bypass by writing to the DB directly. Whatever
    ``resolve_mark`` is wired to read out of an override row's
    ``[marks]`` table MUST still go through that same gate, never a shortcut that trusts this table's
    data because it already passed ``_parse_theme_toml``. Passing the closed token/font grammar is not
    the same claim as "safe to render as SVG".
    """

    __tablename__ = "scribble_theme_overrides"

    id: Mapped[uuid.UUID] = mapped_column(ScribbleUuid, primary_key=True, default=uuid.uuid7)
    # The Theme's selectable name (matches `[identity].name` in `source_toml`) — see the class
    # docstring for why this must be kept in lockstep with the stored TOML rather than trusted blind.
    name: Mapped[str] = mapped_column(String(64), unique=True)
    label: Mapped[str] = mapped_column(String(255))
    # The operator's ORIGINAL TOML text, byte-for-byte — never a parsed/normalized re-serialization.
    # No length cap at the schema layer (Text is unbounded); `scribble.themes_api` enforces a sane
    # request-body ceiling before this ever reaches a session, the same "reject before you touch the
    # DB" shape `reporting.theme_files.MAX_EMBEDDED_FONT_BYTES` uses for font payloads.
    source_toml: Mapped[str] = mapped_column(Text)
    created_by: Mapped[str | None] = mapped_column(String(128))
    updated_by: Mapped[str | None] = mapped_column(String(128))


class ScribbleSettings(Base, TimestampMixin):
    """Install-wide Scribble configuration — a singleton row, one per Scribble install (ext#105).

    ``slot`` is unique and always ``"default"``, the same pattern as CREAM's ``Brand``
    (``cream/cream/models.py``) and Vector's own per-install knobs: a setting that applies to every
    engagement belongs in the database (edited from the UI; a UNIQUE constraint is what makes
    "exactly one row" enforceable, where a config file can't stop a second deploy writing a second one).

    **Why this is a Scribble-owned table, not the host's generic ``[[settings]]`` seam** (see Vector's
    ``lotek-extension.toml`` for that seam's shape, and ``vector/vector/deps.py::host_setting``): this
    checkout's own ``scribble/deps.py`` carries no ``host_setting``/``extras["extension_setting"]``
    accessor and ``scribble/lotek-extension.toml`` declares no ``[[settings]]`` table — the seam Vector
    already uses simply does not exist in Scribble yet, and neither file is this ticket's to create
    (``lotek-extension.toml``/``deps.py`` are the orchestrator's integration points, not
    ``models.py``/``themes_api.py``). ext#100/#105/#106's own investigation record (commit ``75159ed``)
    anticipated this might not be needed "if the host seam exists" — it does not, HERE, so this table is
    the minimal singleton the same note prescribed as the fallback. If ``scribble/deps.py`` later grows
    ``host_setting`` the way Vector's has it, migrating ``default_report_theme`` there and dropping this
    table is a natural follow-up; nothing about the shape below blocks that.

    **Why not fold this into ``TemplateVariable``?** ``TemplateVariable`` rows are ``{{VARIABLES}}``
    interpolated into report BODY TEXT — an entirely different axis from "which Theme does an install
    default to". Storing the default Theme name as a ``TemplateVariable`` would re-collapse the exact
    vocabulary this whole effort exists to untangle (see ``scribble/CONTEXT.md``): Layout, Theme, and
    Token would once again share one bucket with report prose.

    Kept deliberately small: ``default_report_theme`` is the only knob #105 asks for. A later ticket
    earns its own column the same way this one did — do not pre-add speculative settings nobody asked
    for.
    """

    __tablename__ = "scribble_theme_settings"

    id: Mapped[uuid.UUID] = mapped_column(ScribbleUuid, primary_key=True, default=uuid.uuid7)
    slot: Mapped[str] = mapped_column(String(16), unique=True, default="default")

    # The per-install default Theme NAME (`scribble.reporting.themes.ReportTheme.name`, a bundled Theme
    # name today; an override or installed Theme name once selection is wired to consult them), used
    # whenever an Engagement's own Theme choice is unset. No FK/no Enum — same reasoning as the cut
    # `Engagement.report_theme` column this recovers from (`75159ed`): the set of valid names is a
    # Python-level registry (plus, after this ticket, `ScribbleThemeOverride.name`) that can grow
    # without a schema change, so this column only stores the chosen name; resolving it safely (falling
    # back for an unknown/removed name) is the reader's job, same as an untrusted `?theme=` query value.
    # NULL is a legal, meaningful value: "no install override; fall back to
    # `reporting.themes.DEFAULT_THEME`" — not merely "not configured yet".
    default_report_theme: Mapped[str | None] = mapped_column(String(64), nullable=True)


# --------------------------------------------------------------------------- collaboration (Phase B)


class CollabDoc(Base, TimestampMixin):
    """Persisted Yjs CRDT state for a finding content block (Phase B). Defined now to freeze schema."""

    __tablename__ = "scribble_collab_docs"
    __table_args__ = (UniqueConstraint("finding_id", "block"),)

    id: Mapped[uuid.UUID] = mapped_column(ScribbleUuid, primary_key=True, default=uuid.uuid7)
    finding_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("scribble_findings.id"))
    block: Mapped[str] = mapped_column(String(64))
    ydoc_state: Mapped[bytes | None] = mapped_column()
    updated_at_ms: Mapped[int | None] = mapped_column(Integer)


# --------------------------------------------------------------------------- checklists

# NOTE: Integer PKs match the rest of Scribble's model (Client/Engagement/Finding). Checklists are
# NON-BLOCKING visual reminders: no state here gates an operation or withholds a report. See
# plans/SCRIBBLE_CHECKLISTS.md (in lotek) for the full design.


class ChecklistTemplate(Base, TimestampMixin):
    """A reusable checklist definition (the library entry). ``builtin`` rows are seeded and editable in
    place; ``customized`` flips once one is edited (drives a "modified from default" hint + Reset);
    ``hidden`` drops it from the picker without deleting it."""

    __tablename__ = "scribble_checklist_templates"

    id: Mapped[uuid.UUID] = mapped_column(ScribbleUuid, primary_key=True, default=uuid.uuid7)
    slug: Mapped[str] = mapped_column(String(128), unique=True)
    name: Mapped[str] = mapped_column(String(255))
    description: Mapped[str | None] = mapped_column(Text)
    kind: Mapped[ChecklistKind] = mapped_column(Enum(ChecklistKind), default=ChecklistKind.coverage)
    category: Mapped[str | None] = mapped_column(String(255))  # suggest-by-assessment-type hint
    builtin: Mapped[bool] = mapped_column(Boolean, default=False)
    customized: Mapped[bool] = mapped_column(Boolean, default=False, nullable=True)
    hidden: Mapped[bool] = mapped_column(Boolean, default=False, nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)

    items: Mapped[list[ChecklistTemplateItem]] = relationship(
        back_populates="template",
        cascade="all, delete-orphan",
        order_by="ChecklistTemplateItem.order_index",
    )


class ChecklistTemplateItem(Base, TimestampMixin):
    """One item in a template. ``framework``/``control_ref`` (free text) power the compliance
    attestation appendix. ``guidance`` is the how-to-test prose."""

    __tablename__ = "scribble_checklist_template_items"

    id: Mapped[uuid.UUID] = mapped_column(ScribbleUuid, primary_key=True, default=uuid.uuid7)
    template_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("scribble_checklist_templates.id"))
    order_index: Mapped[int] = mapped_column(Integer, default=0)
    section: Mapped[str | None] = mapped_column(String(255))
    text: Mapped[str] = mapped_column(String(512))
    guidance: Mapped[str | None] = mapped_column(Text)
    framework: Mapped[str | None] = mapped_column(String(128))
    control_ref: Mapped[str | None] = mapped_column(String(128))
    default_status: Mapped[str | None] = mapped_column(String(64))

    template: Mapped[ChecklistTemplate] = relationship(back_populates="items")


class EngagementChecklist(Base, TimestampMixin):
    """A checklist ASSIGNED to an engagement (the 0..N join). A SNAPSHOT: items are copied from the
    template on assign, so a later template edit never rewrites a delivered engagement. ``template_id``
    is a soft reference (provenance only), not an FK, so deleting a library template never touches an
    assigned copy."""

    __tablename__ = "scribble_engagement_checklists"

    id: Mapped[uuid.UUID] = mapped_column(ScribbleUuid, primary_key=True, default=uuid.uuid7)
    engagement_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("scribble_engagements.id"))
    # Soft ref (provenance) to `scribble_checklist_templates.id` — an INTRA-Scribble reference that never
    # declared its ForeignKey, which is why the UUID migration's FK sweep does not see it and why it has
    # to be typed by hand. It points at a Scribble PK, so it follows them to UUIDv7 (lotek#335).
    template_id: Mapped[uuid.UUID | None] = mapped_column(ScribbleUuid, nullable=True)
    name: Mapped[str] = mapped_column(String(255))
    kind: Mapped[ChecklistKind] = mapped_column(Enum(ChecklistKind), default=ChecklistKind.coverage)
    include_in_report: Mapped[bool] = mapped_column(Boolean, default=True)
    order_index: Mapped[int] = mapped_column(Integer, default=0)
    assigned_by: Mapped[str | None] = mapped_column(String(128))

    engagement: Mapped[Engagement] = relationship(back_populates="checklists")
    items: Mapped[list[EngagementChecklistItem]] = relationship(
        back_populates="checklist",
        cascade="all, delete-orphan",
        order_by="EngagementChecklistItem.order_index",
    )


class EngagementChecklistItem(Base, TimestampMixin):
    """Per-assignment item state (copied from the template on assign). ``status`` is FREE TEXT (the UI
    offers the kind's recommended values as a dropdown but accepts a custom label); rollup buckets it.
    ``finding_id`` links a failed coverage item to the finding that documents it."""

    __tablename__ = "scribble_engagement_checklist_items"

    id: Mapped[uuid.UUID] = mapped_column(ScribbleUuid, primary_key=True, default=uuid.uuid7)
    engagement_checklist_id: Mapped[int] = mapped_column(
        ForeignKey("scribble_engagement_checklists.id")
    )
    order_index: Mapped[int] = mapped_column(Integer, default=0)
    section: Mapped[str | None] = mapped_column(String(255))
    text: Mapped[str] = mapped_column(String(512))
    guidance: Mapped[str | None] = mapped_column(Text)
    framework: Mapped[str | None] = mapped_column(String(128))
    control_ref: Mapped[str | None] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(64), default="pending")
    note: Mapped[str | None] = mapped_column(Text)
    finding_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("scribble_findings.id"), nullable=True)
    updated_by: Mapped[str | None] = mapped_column(String(128))

    checklist: Mapped[EngagementChecklist] = relationship(back_populates="items")


__all__ = [
    "Client",
    "Engagement",
    "AssessmentType",
    "FindingGroup",
    "VulnerabilityTemplate",
    "ScribbleVulnMap",
    "EngagementFinding",
    "Artifact",
    "EngagementDiagram",
    "TemplateVariable",
    "VariableValue",
    "Tag",
    "FindingTag",
    "TemplateTag",
    "ReportTemplate",
    "ReportRender",
    "ScribbleThemeOverride",
    "ScribbleSettings",
    "CollabDoc",
    "ChecklistTemplate",
    "ChecklistTemplateItem",
    "EngagementChecklist",
    "EngagementChecklistItem",
]

# Register the v2-native enrichment table (scribble_enrichment_proposals) on Scribble's Base — imported
# for its side effect so create_all() (which imports scribble.models) creates it. See scribble/enrichment.py.
from scribble import enrichment  # noqa: E402, F401  (side-effect registration)

