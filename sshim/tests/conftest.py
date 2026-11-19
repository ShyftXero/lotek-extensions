"""Shared fixtures: an in-memory SQLite DB with sshim's tables, and a session factory.

A stub DB proves sshim's own LOGIC (tenancy, regeneration, liveness). It does NOT prove the MOUNT —
the host seam (PAT identity -> g.principal, CSRF/session exemption) only truly exists inside lotek, so
the mounted contract is pinned in lotek's own `test_sshim_extension.py`, not here.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import create_engine

from sshim import secretbox
from sshim.db import Base, make_session_factory


@pytest.fixture
def engine():
    eng = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    return eng


@pytest.fixture
def session_factory(engine):
    return make_session_factory(engine)


@pytest.fixture
def db(session_factory):
    with session_factory() as s:
        yield s


@pytest.fixture
def key_path(tmp_path):
    """The instance-dir key that seals a session's password (`sshim.secretbox`). A tmp dir keeps every
    test's ciphertext unreadable to every other test's key — the same fail-closed shape as production."""
    return secretbox.key_path(tmp_path)


@pytest.fixture
def owner():
    return uuid.uuid7()


@pytest.fixture
def other():
    return uuid.uuid7()
