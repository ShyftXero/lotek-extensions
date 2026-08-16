"""Read-only context sidecar for the scribble-report-refine skill.

Bridges a Scribble engagement's on-disk state into the same ``ReportContext`` shape the report
renderers consume (``scribble.reporting.context.build_report_context``), as plain, JSON-serializable
data -- WITHOUT ever opening a writable connection to the database. This is the ONLY sanctioned way for
the skill to read engagement facts (see ``../SKILL.md``'s no-data-change guardrail): every read here
goes through a session bound to a connection SQLite itself opened in ``mode=ro``, enforced at the
connection level rather than by caller convention, so a coding mistake that tries to write through this
session fails loudly instead of silently mutating a client's report.

Caveat: this script runs outside any Flask app context, so ``scribble.deps.client_model()`` -- used
transitively by ``build_report_context`` when resolving a finding's client -- always falls back to
Scribble's OWN client table (``scribble_clients``), never a mounted host's client table. That is a
documented pre-existing behavior of ``client_model()`` (see ``scribble/deps.py``), not something this
sidecar introduces.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from urllib.parse import quote

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

from scribble.models import Engagement
from scribble.reporting.context import build_report_context

__all__ = ["open_readonly_session", "build_sidecar_dict", "select", "Engagement", "main"]


def _readonly_engine(db_path: str | Path):
    """A SQLAlchemy engine bound to ``db_path``, opened via SQLite's own URI ``mode=ro`` -- read-only
    enforced by SQLite itself at the connection level, not merely by this module choosing never to call
    ``session.commit()`` after a write.

    The resolved path is percent-encoded before being embedded in the ``file:`` URI: SQLite's URI parser
    treats a handful of characters specially (a bare space is invalid there; ``#`` starts a fragment),
    and an unescaped one would truncate the path -- worse, an unescaped ``#`` swallows everything after
    it, including the ``?mode=ro`` query string, silently reopening the file read-write instead of
    failing loudly. Quoting the whole resolved path (``/`` kept unescaped) sidesteps all of that.
    """
    resolved = Path(db_path).resolve()
    uri = f"file:{quote(str(resolved), safe='/')}?mode=ro"

    def _connect():
        return sqlite3.connect(uri, uri=True)

    # NullPool: every ``open_readonly_session`` call gets a genuinely fresh connection rather than one
    # potentially cached from an earlier engine/path -- the read-only guarantee is re-established every
    # time, never inherited from a pooled connection opened for a different db_path.
    return create_engine("sqlite://", creator=_connect, poolclass=NullPool, future=True)


@contextmanager
def open_readonly_session(db_path: str | Path) -> Iterator[Session]:
    """Yield a SQLAlchemy session over ``db_path`` that cannot write -- see ``_readonly_engine``.

    A plain read (``session.execute(select(...))``) works exactly as normal; any write attempt
    (``session.add(...)`` followed by ``session.commit()``) raises ``sqlalchemy.exc.OperationalError``
    straight from SQLite itself ("attempt to write a readonly database"). Every caller in this file --
    and any future tooling this skill grows -- must route ALL engagement reads through this session and
    never substitute a writable one; there is no code path here that would let a write through it
    succeed.
    """
    engine = _readonly_engine(db_path)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    session = session_factory()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _find_engagement(session: Session, *, engagement_id=None, engagement_name=None) -> Engagement:
    if engagement_id is not None:
        engagement = session.get(Engagement, engagement_id)
    elif engagement_name is not None:
        engagement = session.scalar(select(Engagement).where(Engagement.name == engagement_name))
    else:
        raise LookupError("build_sidecar_dict requires engagement_id or engagement_name")
    if engagement is None:
        ident = engagement_id if engagement_id is not None else engagement_name
        raise LookupError(f"no scribble engagement matches {ident!r}")
    return engagement


def build_sidecar_dict(
    session: Session, engagement_id: int | None = None, engagement_name: str | None = None
) -> dict:
    """Build the plain-dict, JSON-serializable sidecar the skill's input contract promises.

    Shaped exactly like ``scribble.reporting.context.ReportContext`` (the same object the HTML/DOCX
    renderers build from) -- ``engagement_id``/``engagement_name`` at the top, a ``groups`` list (each
    with ``id``/``name``/``type_slug``/``findings``), a severity ``rollup`` (``counts``/``total``/
    ``overall``), and the resolved template ``variables``. Every ``EngagementFinding``'s ``severity``,
    ``cvss_score``/``cvss_vector``, and ``artifacts`` come through untouched from the database -- this
    function performs no write of any kind, and raises ``LookupError`` rather than returning ``None``
    or an empty dict when neither identifier resolves to a real engagement.
    """
    engagement = _find_engagement(session, engagement_id=engagement_id, engagement_name=engagement_name)
    context = build_report_context(engagement)
    return asdict(context)


def main(args: list[str]) -> int:
    """CLI entry point: write the sidecar JSON for one engagement to ``--out``. Returns ``0`` on
    success; a missing engagement raises ``LookupError`` rather than being swallowed into a nonzero
    exit, so a caller can't mistake "engagement not found" for "wrote an empty report"."""
    parser = argparse.ArgumentParser(
        prog="context_sidecar",
        description=(
            "Read-only Scribble engagement -> ReportContext-shaped JSON, for the scribble-report-refine "
            "skill. Never writes to the database."
        ),
    )
    parser.add_argument("--db", required=True, help="Path to the Scribble SQLite database file.")
    parser.add_argument("--engagement-id", type=int, default=None, help="Engagement id to look up.")
    parser.add_argument(
        "--engagement-name",
        default=None,
        help="Engagement name to look up (alternative to --engagement-id).",
    )
    parser.add_argument("--out", required=True, help="Path to write the sidecar JSON file to.")
    ns = parser.parse_args(args)

    with open_readonly_session(ns.db) as session:
        data = build_sidecar_dict(
            session, engagement_id=ns.engagement_id, engagement_name=ns.engagement_name
        )

    Path(ns.out).write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
