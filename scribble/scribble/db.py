"""SQLAlchemy base + session helpers.

Scribble owns its own declarative ``Base`` when standalone. When mounted in Lotek, ``register()`` is
handed the host engine/session factory and simply creates Scribble's tables in the shared database; the
model *shapes* (not the Base identity) are what align with Lotek. Reconciling to a single ``Base`` is a
port-checkpoint concern, deliberately out of scope here.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import DateTime, String, TypeDecorator
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class SoftHostId(TypeDecorator):
    """A HOST soft-ref id (``Engagement.owner_id`` / ``.client_id``) that must hold either shape the host
    may use: a plain sequential int (standalone Scribble's own tables, and any pre-v2/legacy mounted
    host) OR a ``uuid.UUID`` (Lotek v2's UUIDv7 surrogate PKs) -- see docs/LOTEK_ADOPTION.md §3.1/§4.
    There is no FK (the referenced table isn't known until mount time -- ``scribble.deps.client_model``),
    so the column only needs to store + faithfully round-trip whatever id shape the host actually uses.

    Stored as TEXT (portable across SQLite/Postgres); ``process_result_value`` reconstructs the ORIGINAL
    Python type on read (int for a digit string, ``uuid.UUID`` for a UUID-shaped one) rather than always
    handing back a string. That round-trip is load-bearing, not cosmetic: a plain string passed to
    ``session.get()``/``.in_()`` against a ``sqlalchemy.Uuid``-typed host PK raises (the Uuid type expects
    a real ``uuid.UUID`` object), so returning a string here would silently break ``Engagement.
    resolve_client`` and ``scribble.deps.client_names`` for a UUID host even though the id itself
    "persisted" -- and returning a string instead of the original int would just as silently break every
    existing int-id equality check (``engagement.owner_id == some_user.id``).

    Schema-history caveat (mirrors ``Engagement.client_id``'s FK-removal note above): ``scribble.db.
    create_all`` only ADDS columns to a pre-existing table, it never retrofits an existing column's
    declared type. A table created before this change has ``owner_id``/``client_id`` as a native INTEGER
    column; on SQLite that's harmless (no real type enforcement, so a UUID string still stores), but on
    Postgres inserting a UUID string into an INTEGER column raises. A pre-existing Postgres-backed mount
    needs a one-time manual ``ALTER TABLE scribble_engagements ALTER COLUMN owner_id/client_id TYPE
    VARCHAR(64)`` before mounting under a UUID host -- there is no migration framework here to do it
    automatically. A freshly created database (the common case, and every test) needs nothing.
    """

    impl = String(64)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        return str(value)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        # Try int() rather than `value.isdigit()`: isdigit() is False for a leading '-', so a negative
        # id (a host's "system"/sentinel actor, e.g. id=-1) would silently round-trip as the STRING
        # "-1" instead of the int -1 -- the exact silent-attribution-loss bug this type exists to fix,
        # reintroduced at the edge (`owner_id == -1` would just quietly stop matching).
        try:
            return int(value)
        except ValueError:
            pass
        try:
            return uuid.UUID(value)
        except ValueError:
            return value  # opaque id shape we don't recognize -- hand it back as-is rather than raise


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


def make_session_factory(engine) -> sessionmaker:
    return sessionmaker(bind=engine, expire_on_commit=False)


#: Table-name suffixes Scribble owns (real names are ``scribble_<suffix>``). Kept in sync with
#: ``models.py`` __tablename__s; used only by the one-shot fraction->scribble rename below.
_TABLE_SUFFIXES = (
    "clients", "engagements", "assessment_types", "finding_groups", "vuln_templates",
    "vuln_map", "findings", "artifacts", "variables", "variable_values", "tags",
    "finding_tags", "template_tags", "report_templates", "report_renders", "collab_docs",
    "checklist_templates", "checklist_template_items", "engagement_checklists",
    "engagement_checklist_items",
)


def _rename_from_fraction(engine) -> None:
    """One-shot, idempotent, data-preserving rename of this extension's tables from the old
    ``fraction_`` prefix to ``scribble_`` (the extension was renamed Fraction -> Scribble).

    Runs at the TOP of ``create_all`` — BEFORE ``Base.metadata.create_all`` — so it renames the
    POPULATED tables in place rather than letting create_all build empty ``scribble_`` tables beside
    the old data. Guarded per table (rename only when the old name exists and the new one does not), so
    it is a no-op on a fresh database and on every boot after the first. On Postgres — the real backend
    — ``RENAME TO`` carries foreign-key constraints across automatically; index/sequence names keep
    their old ``fraction_``-prefixed labels (cosmetic, still functional). The core ``jobs`` table's
    ``promoted_extension`` value ('fraction' -> 'scribble') is the host's to migrate, not this module's.
    """
    from sqlalchemy import inspect, text

    existing = set(inspect(engine).get_table_names())
    with engine.begin() as conn:
        for suffix in _TABLE_SUFFIXES:
            old, new = f"fraction_{suffix}", f"scribble_{suffix}"
            if old in existing and new not in existing:
                conn.execute(text(f'ALTER TABLE "{old}" RENAME TO "{new}"'))


def _remap_standalone_client_ids(engine) -> None:
    """One-shot, idempotent remap of ``scribble_engagements.client_id`` from standalone
    ``scribble_clients`` ids to the HOST client table's ids — closing a report-authz IDOR.
    ``Engagement.client_id`` is a *soft* int reference that carries no id-space, so a scribble-space id
    in a standalone->mounted DB is misread as a host ``clients.id`` and can collide with a real host
    client an attacker owns a job under (both are small sequential ints), exposing that client's report.

    Runs at the END of ``create_all`` (tables exist). Guards fire it ONLY on the reachable case and make
    it safe + idempotent everywhere else — the load-bearing insight is that **in mounted mode Scribble
    writes the HOST client table, never ``scribble_clients``**, so rows in ``scribble_clients`` ⟺ the DB
    carries standalone history whose ``client_id``s are scribble-space:

    * STANDALONE (``client_model()`` is Scribble's own ``Client``) -> return: ``scribble_clients`` IS the
      real store; nothing to remap.
    * MOUNTED + ``scribble_clients`` empty -> return: fresh mounted install; never touch valid host ids.
    * MOUNTED + ``scribble_clients`` has rows -> remap each engagement whose ``client_id`` matches a
      ``scribble_clients`` row to the host client of the SAME NAME (or NULL if no name match -> admin-only
      report, the secure default), then rename ``scribble_clients`` away so the next boot's create_all
      recreates it empty and this no-ops (idempotent). ``scribble_clients`` has no FK dependents (the ref
      is a soft int), so the rename is safe.

    Residual (documented): if mounted engagements were added BEFORE this first ran, a host-space
    ``client_id`` coincidentally equal to a ``scribble_clients`` row id is falsely remapped — bounded,
    one-time, and absent on a normal cutover (mount -> first boot remaps -> then add engagements). v2's
    per-engagement membership replaces this whole path and should delete it.
    """
    import logging

    from sqlalchemy import inspect, text

    from scribble import models
    from scribble.deps import client_model

    host_model = client_model()
    if host_model is models.Client:
        return  # standalone: scribble_clients is the real client store
    existing = set(inspect(engine).get_table_names())
    if "scribble_clients" not in existing or "scribble_engagements" not in existing:
        return
    host_table = host_model.__tablename__
    with engine.begin() as conn:
        scribble_rows = conn.execute(text("SELECT id, name FROM scribble_clients")).fetchall()
        if not scribble_rows:
            return  # fresh mounted DB — scribble_clients was never written; nothing to remap
        smap = {r[0]: r[1] for r in scribble_rows}
        name_to_host = {r[1]: r[0] for r in conn.execute(text(f'SELECT id, name FROM "{host_table}"'))}
        remapped = nulled = 0
        for eid, cid in conn.execute(
            text("SELECT id, client_id FROM scribble_engagements WHERE client_id IS NOT NULL")
        ).fetchall():
            # client_id is now `SoftHostId` (TEXT-backed, to also hold a v2 UUID host id) -- a raw SQL
            # fetch (bypassing the ORM's type decoder) hands back whatever the driver's storage/affinity
            # gives it, which for a plain int written earlier may come back as a str. `smap`'s keys are
            # always genuine ints (scribble_clients.id is its own unrelated Integer PK), so normalize
            # before comparing; a real UUID-shaped cid just fails int() and correctly falls through as
            # "not a known scribble-space id" (a UUID can never collide with a scribble-space int).
            try:
                cid_key = int(cid)
            except (TypeError, ValueError):
                cid_key = cid
            if cid_key not in smap:
                continue  # not a known scribble-space id -> leave (host-space, or already remapped)
            new_id = name_to_host.get(smap[cid_key])
            conn.execute(
                text("UPDATE scribble_engagements SET client_id = :n WHERE id = :e"),
                {"n": new_id, "e": eid},
            )
            nulled += new_id is None
            remapped += new_id is not None
        # Retire scribble_clients so this is idempotent (next boot's create_all recreates it empty ->
        # the empty-guard above returns). Keep it as a backup unless one already exists.
        if "scribble_clients_pre_mount_remap" not in existing:
            conn.execute(text("ALTER TABLE scribble_clients RENAME TO scribble_clients_pre_mount_remap"))
        else:
            conn.execute(text("DROP TABLE scribble_clients"))
    logging.getLogger("scribble").warning(
        "scribble: remapped %d engagement client refs to host client ids (%d had no host client of the "
        "same name -> admin-only report until re-linked); scribble_clients retired to "
        "scribble_clients_pre_mount_remap",
        remapped,
        nulled,
    )


def create_all(engine) -> None:
    """Create Scribble's tables, then additively add any new columns (and their indexes) to
    pre-existing tables.

    SQLAlchemy's ``create_all`` only ever creates MISSING TABLES — never new COLUMNS on a table that
    already exists, and it skips a pre-existing table entirely (so it won't add a new index either).
    Scribble has no migration framework, so when a model gains a column (e.g. ``Engagement.owner_id``) a
    database that already had the table would be left without it and the next write would fail. This runs
    a minimal, additive, idempotent migration: for each table that ALREADY existed before ``create_all``,
    ``ALTER TABLE ADD COLUMN`` for any model column the DB is missing, then create any declared index on a
    newly added column (mirrors lotek's ``migrate_sqlite``, which pairs ADD COLUMN with CREATE INDEX IF
    NOT EXISTS).

    Scope/limits — additive-only, never drops or renames, safe to run on every mount:
      * Columns: only NULLABLE or DEFAULTED columns are added (a NOT-NULL-no-default column can't be added
        to populated rows, so it's skipped).
      * Constraints: only INDEXes are retrofitted. A UNIQUE/CHECK constraint on a column added this way is
        NOT enforced on upgraded DBs — a column that needs one must be handled explicitly (e.g. a bespoke
        migration), not relied upon through this path.
    """
    from sqlalchemy import inspect, text

    from scribble import models  # noqa: F401

    _rename_from_fraction(engine)  # migrate an old fraction_*-prefixed DB in place, before create_all
    pre_existing = set(inspect(engine).get_table_names())  # capture BEFORE create_all (post-rename)
    Base.metadata.create_all(engine)  # creates missing tables at full shape; no-ops existing ones

    insp = inspect(engine)  # fresh inspector reflecting the post-create_all schema
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if table.name not in pre_existing:
                continue  # freshly created by create_all above — already has all columns + indexes
            have = {c["name"] for c in insp.get_columns(table.name)}
            added = False
            for col in table.columns:
                if col.name in have:
                    continue
                if not col.nullable and col.default is None and col.server_default is None:
                    continue  # can't safely add a NOT NULL column with no default to existing rows
                coltype = col.type.compile(dialect=engine.dialect)
                conn.execute(text(f'ALTER TABLE "{table.name}" ADD COLUMN "{col.name}" {coltype}'))
                have.add(col.name)
                added = True
            if added:
                # create_all skipped this pre-existing table, so an index on a just-added column
                # (e.g. ix_scribble_engagements_owner_id) is missing. checkfirst -> IF NOT EXISTS.
                for index in table.indexes:
                    if all(c.name in have for c in index.columns):
                        index.create(bind=conn, checkfirst=True)

    # Last: close the report-authz IDOR by remapping any standalone-space client_ids to host space.
    # After the tables exist, so it can read/rewrite scribble_engagements + retire scribble_clients.
    _remap_standalone_client_ids(engine)
