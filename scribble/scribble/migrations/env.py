"""Alembic environment for Scribble's OWN schema.

Scribble does not own a database — it is mounted into a host (lotek) and handed that host's engine, so
its migrations run **inside someone else's database, alongside someone else's migration history**. Two
consequences shape this file:

* **A separate version table.** `version_table="scribble_alembic_version"` keeps Scribble's revision
  pointer out of core's `alembic_version`. Sharing one table would make each project's `upgrade head`
  see the other's revision id as an unknown head and refuse — or, worse, stamp over it.
* **No `sqlalchemy.url`, ever.** The engine arrives from the host at mount time. `alembic.ini` carries
  no URL and this file never reads one; an offline/`--sql` run is deliberately unsupported, because
  generating SQL for a database whose identity we do not know is how you apply a migration to the wrong
  one.

Autogenerate is scoped with `include_object`: the host's tables live in the same `MetaData`-less
namespace as far as reflection is concerned, so without this filter `--autogenerate` would cheerfully
emit `DROP TABLE jobs`.
"""

from __future__ import annotations

from alembic import context
from sqlalchemy import engine_from_config, pool

import scribble.models  # noqa: F401  -- REQUIRED: importing db.Base alone leaves metadata EMPTY, and
#                          autogenerate then reports success while emitting an empty revision.
from scribble.db import Base

config = context.config
target_metadata = Base.metadata

#: Scribble owns exactly the tables it declares, all of which carry this prefix. Anything else in the
#: database belongs to the host or to a sibling extension and is NOT ours to alter, drop or reorder.
TABLE_PREFIX = "scribble_"

VERSION_TABLE = "scribble_alembic_version"


def include_object(object_, name, type_, reflected, compare_to):  # noqa: ARG001
    """Keep autogenerate inside Scribble's own prefix.

    Without this, a `--autogenerate` run against a mounted database sees every core and sibling-extension
    table as "in the database but not in my metadata" and proposes dropping them. That is not a
    hypothetical footgun: it is the default behaviour.
    """
    if type_ == "table":
        return name.startswith(TABLE_PREFIX) and name != VERSION_TABLE
    if type_ in ("column", "index", "unique_constraint", "foreign_key_constraint"):
        parent = getattr(object_, "table", None)
        return parent is None or parent.name.startswith(TABLE_PREFIX)
    return True


def run_migrations_online() -> None:
    connectable = config.attributes.get("connection", None)

    if connectable is None:
        # No host-supplied connection: this is a developer running alembic directly, which happens for
        # exactly one reason — autogenerating a revision against a SCRATCH database. Take the URL from
        # `-x url=...` so it can never be baked into alembic.ini and accidentally point at prod.
        url = context.get_x_argument(as_dictionary=True).get("url")
        if not url:
            raise RuntimeError(
                "no database to migrate: pass one with `-x url=postgresql+psycopg://…` (scratch DBs "
                "only), or call scribble.db.run_migrations with the host engine. alembic.ini carries "
                "no sqlalchemy.url on purpose."
            )
        section = config.get_section(config.config_ini_section, {}) or {}
        section["sqlalchemy.url"] = url
        connectable = engine_from_config(section, prefix="sqlalchemy.", poolclass=pool.NullPool)
        with connectable.connect() as connection:
            _configure_and_run(connection)
        return

    _configure_and_run(connectable)


def _configure_and_run(connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        version_table=VERSION_TABLE,
        include_object=include_object,
        # Scribble's tables are the only ones in scope, so a type comparison here cannot propose
        # changes to the host's schema.
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    raise RuntimeError(
        "Scribble migrations do not support offline (--sql) mode: the target database is the HOST's, "
        "supplied at mount time, so there is no URL to generate SQL against. Run them through "
        "scribble.db.run_migrations with the host engine."
    )

run_migrations_online()
