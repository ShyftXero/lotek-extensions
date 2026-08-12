"""Unit tests for ``scribble.db.SoftHostId`` -- the TEXT-backed column type ``Engagement.owner_id``/
``.client_id`` use to hold EITHER host id shape (a plain int, standalone/legacy, or a ``uuid.UUID``,
Lotek v2's UUIDv7 PKs) and round-trip the ORIGINAL Python type on read (see the type's own docstring for
why that round-trip is load-bearing, not cosmetic).

These drive the type directly against a throwaway table -- no Flask app/host needed -- so they pin the
type's own contract independent of any call site (``engagement_ui.py``/``deps.py``, covered by their own
integration tests in ``test_client_model_injection.py``/``test_deps.py``).
"""

from __future__ import annotations

import uuid

from sqlalchemy import Integer, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from scribble.db import SoftHostId


class _Base(DeclarativeBase):
    pass


class _Ref(_Base):
    __tablename__ = "soft_host_id_probe"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ref: Mapped[object | None] = mapped_column(SoftHostId, nullable=True)


def _session_factory(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'probe.db'}", future=True)
    _Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, future=True)


def _round_trip(tmp_path, value):
    sf = _session_factory(tmp_path)
    with sf() as db:
        db.add(_Ref(id=1, ref=value))
        db.commit()
    with sf() as db:
        return db.get(_Ref, 1).ref


def test_positive_int_round_trips_as_int(tmp_path):
    result = _round_trip(tmp_path, 5)
    assert result == 5
    assert isinstance(result, int)


def test_negative_int_round_trips_as_int_not_string(tmp_path):
    """Regression: a naive ``value.isdigit()`` check is False for a leading '-', so a sentinel/system
    actor id like -1 would silently come back as the STRING "-1" -- the exact silent-attribution-loss
    bug this type exists to fix, reintroduced at the edge (``owner_id == -1`` would quietly stop
    matching)."""
    result = _round_trip(tmp_path, -1)
    assert result == -1
    assert isinstance(result, int)


def test_zero_round_trips_as_int(tmp_path):
    result = _round_trip(tmp_path, 0)
    assert result == 0
    assert isinstance(result, int)


def test_uuid_round_trips_as_uuid(tmp_path):
    value = uuid.uuid4()
    result = _round_trip(tmp_path, value)
    assert result == value
    assert isinstance(result, uuid.UUID)


def test_none_round_trips_as_none(tmp_path):
    assert _round_trip(tmp_path, None) is None
