"""EngagementFinding typed columns: references + CVE/CWE/OWASP/threat_intel (map #616, #624/#625)

Promotes the ``references`` and CVE/CWE/OWASP metadata that #617 preserved only in the verbatim
``source_facts`` snapshot up to TYPED columns on ``scribble_findings``:

  * ``references``        JSON list of ``{label, url, source, suppressed}`` value objects (#624)
  * ``cve_ids``          JSON list[str], normalized+deduped (#625) — closes the Class-2 CVE leak
  * ``cwe_ids``          JSON list[str] from ``DTO.facts["cwe"]`` (#625)
  * ``owasp_categories`` JSON list[str] derived from ``cwe_ids`` (#625)
  * ``threat_intel``     JSON, nullable — a dated ``{as_of, source, cves:{kev,epss}}`` snapshot (#625)

All additive + nullable, so it is safe on a populated table: a row promoted before these columns existed
reads NULL, and every read site uses ``finding.<col> or []`` (or a guarded read for ``threat_intel``). A
fresh database never runs this — it is built from the models (which already declare the columns) and
stamped at head; this only backfills a pre-existing deployment. Idempotent (guards on the reflected
columns, no-op once present).

**This is also a MERGE revision.** #617 (``f4c9a1b2e370`` source_facts) and #620 (``f0a1c2d3e4b5``
risk_override) both branched off ``76a1de5a7c83`` and merged without a merge migration, leaving the
Alembic tree with TWO heads — which made ``run_migrations``' ``stamp("head")``/``upgrade("head")`` raise
``Multiple heads are present`` and broke every app-booting test/deploy. Depending on both heads here
reunites the tree into one head while adding the columns.

Revision ID: a7d2c4e6f810
Revises: f0a1c2d3e4b5, f4c9a1b2e370
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a7d2c4e6f810"
# A tuple = a MERGE revision: it unifies the two pre-existing heads (#617 + #620) into one.
down_revision: str | tuple[str, ...] | None = ("f0a1c2d3e4b5", "f4c9a1b2e370")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "scribble_findings"
_COLUMNS = ("references", "cve_ids", "cwe_ids", "owasp_categories", "threat_intel")


def _present() -> set[str]:
    insp = sa.inspect(op.get_bind())
    return {c["name"] for c in insp.get_columns(_TABLE)}


def upgrade() -> None:
    have = _present()
    for col in _COLUMNS:
        if col not in have:
            op.add_column(_TABLE, sa.Column(col, sa.JSON(), nullable=True))


def downgrade() -> None:
    have = _present()
    for col in reversed(_COLUMNS):
        if col in have:
            op.drop_column(_TABLE, col)
