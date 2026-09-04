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

Plain additive migration chained off the current single head ``a7f3b9c1d2e4`` (the merge revision that
already reunited #617's ``f4c9a1b2e370`` and #620's ``f0a1c2d3e4b5``). It is NOT a merge revision: the
two heads are already merged upstream, so depending on both again would fork the tree back into two
heads and break ``run_migrations``' ``stamp/upgrade("head")``.

Revision ID: a7d2c4e6f810
Revises: a7f3b9c1d2e4
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a7d2c4e6f810"
down_revision: str | tuple[str, ...] | None = "a7f3b9c1d2e4"
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
