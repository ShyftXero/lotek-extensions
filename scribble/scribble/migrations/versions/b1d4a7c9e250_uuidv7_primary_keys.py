"""UUIDv7 primary keys for every Scribble table (breaking — lotek#335)

Sequential integer ids make enumeration free, which is trivial IDOR as a *class*: the tenancy predicates
are what actually refuse a request, and an authorization check that is missing, mis-scoped, or silently
not applying degrades from "attacker walks every tenant" to "attacker cannot address the row". Owner
direction 2026-08-16, breaking change explicitly authorized.

Twenty-two primary-key columns across 21 tables, 22 foreign-key edges.

WHY THE ORDER BELOW IS THE CORRECTNESS ARGUMENT
------------------------------------------------
The URLs are hard-broken with **no `legacy_id` column** (decided), so once the integer ids are gone
there is nothing left to answer *"which UUID replaced int 7?"*. Anything that needs that mapping must
consume it while the old and new columns still coexist on the row. Concretely, core's
`jobs.promoted_ref_id` holds Scribble engagement integers — a cross-repo reference that no test in this
repo can see. So this revision **emits `scribble_pk_migration_map` before it drops anything**, and
lotek's own revision consumes it. Reverse those two and the job→report link breaks silently, with
nothing to reconstruct it from.

Phases, per table, all inside one transaction:
  1. add a nullable `<pk>__uuid` column to every table
  2. backfill it with `uuid7()` — generated in Python, not SQL: Postgres gains `uuidv7()` only in 18,
     and the row counts here are trivial (prod: ~190 rows total)
  3. add `<fk>__uuid` to every child and resolve it by joining the parent on the OLD integer, while both
     columns still exist
  4. **write the mapping table** (old int -> new uuid, per table)
  5. drop FK constraints, drop the old integer columns, rename `*__uuid` into place, restore the primary
     keys and foreign keys

`scribble_findings.parent_id` is self-referential — its "parent" is its own table — which phase 3 handles
without special-casing because the join reads the pre-existing integer columns that are still present.

Revision ID: b1d4a7c9e250
Revises: e17599b0880a
"""
from __future__ import annotations

import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import scribble.db  # noqa: F401  -- revisions reference scribble.db.SoftHostId by name

revision: str = "b1d4a7c9e250"
down_revision: str | None = "e17599b0880a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Frozen deliberately, exactly like the baseline: this revision must keep describing the schema as it
#: was AT THIS POINT in history, not as `Base.metadata` happens to look whenever it is next run.
PK_TABLES: list[tuple[str, list[str]]] = [
    ("scribble_assessment_types", ["id"]),
    ("scribble_checklist_templates", ["id"]),
    ("scribble_clients", ["id"]),
    ("scribble_engagements", ["id"]),
    ("scribble_report_templates", ["id"]),
    ("scribble_tags", ["id"]),
    ("scribble_variables", ["id"]),
    ("scribble_vuln_templates", ["id"]),
    ("scribble_checklist_template_items", ["id"]),
    ("scribble_engagement_checklists", ["id"]),
    ("scribble_finding_groups", ["id"]),
    ("scribble_report_renders", ["id"]),
    ("scribble_template_tags", ["template_id", "tag_id"]),
    ("scribble_vuln_map", ["id"]),
    ("scribble_findings", ["id"]),
    ("scribble_artifacts", ["id"]),
    ("scribble_collab_docs", ["id"]),
    ("scribble_engagement_checklist_items", ["id"]),
    ("scribble_finding_tags", ["finding_id", "tag_id"]),
    ("scribble_variable_values", ["id"]),
]
# `scribble_enrichment_proposals` is absent on purpose: it was built to the v2 contract and its PK is
# ALREADY `Uuid`. Migrating it would be a no-op at best and a corruption at worst.

#: (child_table, child_col, parent_table, parent_col, nullable)
FK_EDGES: list[tuple[str, str, str, str, bool]] = [
    ("scribble_checklist_template_items", "template_id", "scribble_checklist_templates", "id", False),
    ("scribble_engagement_checklists", "engagement_id", "scribble_engagements", "id", False),
    ("scribble_finding_groups", "engagement_id", "scribble_engagements", "id", False),
    ("scribble_finding_groups", "assessment_type_id", "scribble_assessment_types", "id", True),
    ("scribble_report_renders", "engagement_id", "scribble_engagements", "id", False),
    ("scribble_template_tags", "template_id", "scribble_vuln_templates", "id", False),
    ("scribble_template_tags", "tag_id", "scribble_tags", "id", False),
    ("scribble_vuln_map", "template_id", "scribble_vuln_templates", "id", False),
    ("scribble_findings", "engagement_id", "scribble_engagements", "id", False),
    ("scribble_findings", "group_id", "scribble_finding_groups", "id", True),
    ("scribble_findings", "template_id", "scribble_vuln_templates", "id", True),
    ("scribble_findings", "parent_id", "scribble_findings", "id", True),
    ("scribble_artifacts", "engagement_id", "scribble_engagements", "id", False),
    ("scribble_artifacts", "finding_id", "scribble_findings", "id", True),
    ("scribble_collab_docs", "finding_id", "scribble_findings", "id", False),
    ("scribble_engagement_checklist_items", "engagement_checklist_id",
     "scribble_engagement_checklists", "id", False),
    ("scribble_engagement_checklist_items", "finding_id", "scribble_findings", "id", True),
    ("scribble_finding_tags", "finding_id", "scribble_findings", "id", False),
    ("scribble_finding_tags", "tag_id", "scribble_tags", "id", False),
    ("scribble_variable_values", "variable_id", "scribble_variables", "id", False),
    ("scribble_variable_values", "engagement_id", "scribble_engagements", "id", True),
    ("scribble_variable_values", "finding_id", "scribble_findings", "id", True),
]

MAP_TABLE = "scribble_pk_migration_map"
SUFFIX = "__uuid"


#: Postgres truncates identifiers at 63 bytes. Names are TRUNCATED here rather than left to the server,
#: so what this migration creates is what a later migration can find by the same rule.
PG_IDENT_MAX = 63


def _ident(name: str) -> str:
    return name[:PG_IDENT_MAX]


def _tmp(col: str) -> str:
    return f"{col}{SUFFIX}"


def upgrade() -> None:
    conn = op.get_bind()

    # ── 1 + 2. add the uuid column and backfill it -------------------------------------------------
    for table, pks in PK_TABLES:
        for pk in pks:
            op.add_column(table, sa.Column(_tmp(pk), sa.Uuid(), nullable=True))

    # A composite PK's members are themselves foreign keys (the join tables), so they are resolved in
    # phase 3 from their parents rather than minted here — minting a fresh uuid for `finding_tags.
    # finding_id` would point the row at nothing.
    for table, pks in PK_TABLES:
        if len(pks) != 1:
            continue
        pk = pks[0]
        rows = conn.execute(sa.text(f'SELECT "{pk}" FROM "{table}"')).fetchall()
        for (old_id,) in rows:
            conn.execute(
                sa.text(f'UPDATE "{table}" SET "{_tmp(pk)}" = :new WHERE "{pk}" = :old'),
                {"new": uuid.uuid7(), "old": old_id},
            )

    # ── 3. resolve every FK against its parent, while BOTH id columns still exist -------------------
    for child, child_col, parent, parent_col, _nullable in FK_EDGES:
        if child_col not in [c for t, pks in PK_TABLES if t == child for c in pks]:
            op.add_column(child, sa.Column(_tmp(child_col), sa.Uuid(), nullable=True))
        conn.execute(
            sa.text(
                f'UPDATE "{child}" AS c SET "{_tmp(child_col)}" = p."{_tmp(parent_col)}" '
                f'FROM "{parent}" AS p WHERE c."{child_col}" = p."{parent_col}"'
            )
        )

    # ── 4. the mapping table — written BEFORE anything is dropped ----------------------------------
    # This is the only record of old->new that will exist. lotek's revision reads it to rewrite
    # `jobs.promoted_ref_id`, then drops it. Losing it means losing every job->report link with no way
    # back, so it is created here and NOT dropped by this revision.
    op.create_table(
        MAP_TABLE,
        sa.Column("table_name", sa.String(length=64), nullable=False),
        sa.Column("old_int_id", sa.Integer(), nullable=False),
        sa.Column("new_uuid", sa.Uuid(), nullable=False),
        sa.PrimaryKeyConstraint("table_name", "old_int_id"),
    )
    for table, pks in PK_TABLES:
        if len(pks) != 1:
            continue  # a composite join-table row has no single surrogate id to map
        pk = pks[0]
        conn.execute(
            sa.text(
                f'INSERT INTO "{MAP_TABLE}" (table_name, old_int_id, new_uuid) '
                f'SELECT :t, "{pk}", "{_tmp(pk)}" FROM "{table}" WHERE "{_tmp(pk)}" IS NOT NULL'
            ),
            {"t": table},
        )

    # ── 5. swap ------------------------------------------------------------------------------------
    # Drop the FK constraints first: a column cannot be dropped while one depends on it.
    #
    # Constraint names are DISCOVERED, never guessed. Postgres truncates identifiers at 63 bytes, and
    # the conventional `<child>_<col>_fkey` overflows that for at least one table here
    # (`scribble_engagement_checklist_items_engagement_checklist_id_fkey` is 64). A guessed name simply
    # fails to match, `DROP ... IF EXISTS` reports success, and the failure surfaces later and
    # elsewhere as "cannot drop constraint ..._pkey: DependentObjectsStillExist" — which is exactly how
    # this was found.
    insp = sa.inspect(conn)
    for table in sorted({e[0] for e in FK_EDGES}):
        for fk in insp.get_foreign_keys(table):
            if fk.get("name"):
                conn.execute(sa.text(f'ALTER TABLE "{table}" DROP CONSTRAINT "{fk["name"]}"'))

    for table, _pks in PK_TABLES:
        pk = insp.get_pk_constraint(table)
        if pk and pk.get("name"):
            conn.execute(sa.text(f'ALTER TABLE "{table}" DROP CONSTRAINT "{pk["name"]}"'))

    # Old integer columns go; the uuid columns take their names.
    for child, child_col, _p, _pc, nullable in FK_EDGES:
        if child_col in [c for t, pks in PK_TABLES if t == child for c in pks]:
            continue  # handled with the PK swap below
        op.drop_column(child, child_col)
        op.alter_column(child, _tmp(child_col), new_column_name=child_col, nullable=nullable)

    for table, pks in PK_TABLES:
        for pk in pks:
            op.drop_column(table, pk)
            op.alter_column(table, _tmp(pk), new_column_name=pk, nullable=False)
        op.create_primary_key(_ident(f"{table}_pkey"), table, pks)

    for child, child_col, parent, parent_col, _n in FK_EDGES:
        op.create_foreign_key(
            _ident(f"{child}_{child_col}_fkey"), child, parent, [child_col], [parent_col]
        )


def downgrade() -> None:
    # Not reversible, and pretending otherwise would be worse than refusing. The integer ids were
    # dropped; regenerating a sequence would invent identifiers that never existed, silently repointing
    # every external reference (`jobs.promoted_ref_id`, bookmarked URLs, exported reports) at the wrong
    # rows. Restore from a backup taken before the upgrade instead.
    raise NotImplementedError(
        "irreversible: the integer ids are gone and cannot be recovered from the UUIDs. Restore the "
        "pre-migration backup rather than downgrading."
    )
