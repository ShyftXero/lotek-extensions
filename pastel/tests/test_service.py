"""Service-layer tests — domain logic without Flask.

Covers: create, load, expiry, read-count limit, password, owner exemption, delete.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from sqlalchemy import create_engine

import pastel
from pastel.db import utcnow
from pastel.service import (
    Denied,
    Gone,
    Invalid,
    PasswordRequired,
    WrongPassword,
    create,
    delete_paste,
    list_own,
    load_by_slug,
    load_for_read,
)


@pytest.fixture
def db(tmp_path):
    from flask import Flask
    from sqlalchemy.orm import sessionmaker
    app = Flask(__name__)
    engine = create_engine(f"sqlite:///{tmp_path / 'svc_test.db'}", future=True)
    pastel.register(app, engine, instance_path=str(tmp_path))
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session:
        yield session


def _owner_id():
    return uuid.uuid7()


# ---------------------------------------------------------------------------
# create


def test_create_basic(db):
    paste = create(db, owner_id=_owner_id(), owner_name="alice", body="hello world", standalone=False)
    assert paste.slug
    assert len(paste.slug) >= 40  # token_urlsafe(32) → 43 chars
    assert paste.body == "hello world"
    assert paste.read_count == 0
    assert paste.password_hash is None
    assert paste.expires_at is None
    assert paste.read_limit is None
    assert paste.retired_at is None


def test_create_requires_body(db):
    with pytest.raises(Invalid):
        create(db, owner_id=_owner_id(), owner_name="alice", body="", standalone=False)


def test_create_requires_owner_when_mounted(db):
    with pytest.raises(Denied):
        create(db, owner_id=None, owner_name=None, body="hi", standalone=False)


def test_create_allows_no_owner_standalone(db):
    paste = create(db, owner_id=None, owner_name=None, body="hi", standalone=True)
    assert paste.slug


def test_create_with_password(db):
    paste = create(
        db, owner_id=_owner_id(), owner_name="x", body="secret", password="hunter2", standalone=False
    )
    assert paste.password_hash is not None
    assert "hunter2" not in paste.password_hash


def test_create_with_read_limit_validation(db):
    with pytest.raises(Invalid):
        create(db, owner_id=_owner_id(), owner_name="x", body="hi", read_limit=0, standalone=False)


# ---------------------------------------------------------------------------
# expiry


def test_paste_not_expired(db):
    future = utcnow() + timedelta(hours=1)
    owner = _owner_id()
    paste = create(db, owner_id=owner, owner_name="x", body="fresh", expires_at=future, standalone=False)
    loaded = load_for_read(db, paste.slug, is_owner=False)
    assert loaded.slug == paste.slug


def test_paste_expired_raises_gone(db):
    past = utcnow() - timedelta(seconds=1)
    paste = create(db, owner_id=_owner_id(), owner_name="x", body="old", expires_at=past, standalone=False)
    with pytest.raises(Gone):
        load_for_read(db, paste.slug, is_owner=False)


def test_expired_paste_retires_lazily(db):
    from sqlalchemy import select

    from pastel.models import Paste
    past = utcnow() - timedelta(seconds=1)
    paste = create(db, owner_id=_owner_id(), owner_name="x", body="old", expires_at=past, standalone=False)
    with pytest.raises(Gone):
        load_for_read(db, paste.slug)
    row = db.scalars(select(Paste).where(Paste.id == paste.id)).one()
    assert row.retired_at is not None
    assert row.retired_reason == "expired"


# ---------------------------------------------------------------------------
# read-count limit


def test_read_limit_counts(db):
    paste = create(db, owner_id=_owner_id(), owner_name="x", body="limited", read_limit=3, standalone=False)
    for _ in range(3):
        load_for_read(db, paste.slug, is_owner=False)
    # 4th read should raise Gone (paste was retired after 3rd)
    with pytest.raises(Gone):
        load_for_read(db, paste.slug, is_owner=False)


def test_read_limit_owner_exempted(db):
    owner = _owner_id()
    paste = create(db, owner_id=owner, owner_name="x", body="limited", read_limit=1, standalone=False)
    # Owner previews do not count
    for _ in range(5):
        load_for_read(db, paste.slug, is_owner=True)
    # count still 0, paste alive
    fresh = load_by_slug(db, paste.slug)
    assert fresh.read_count == 0
    assert fresh.retired_at is None


def test_read_limit_retire_on_last_read(db):
    from sqlalchemy import select

    from pastel.models import Paste
    paste = create(db, owner_id=_owner_id(), owner_name="x", body="once", read_limit=1, standalone=False)
    # First (last) read should succeed
    result = load_for_read(db, paste.slug, is_owner=False)
    assert result.body == "once"
    # Paste must now be retired
    row = db.scalars(select(Paste).where(Paste.id == paste.id)).one()
    assert row.retired_reason == "read_limit"


# ---------------------------------------------------------------------------
# password


def test_password_required_raises(db):
    paste = create(
        db, owner_id=_owner_id(), owner_name="x", body="secret", password="pass123", standalone=False
    )
    with pytest.raises(PasswordRequired):
        load_for_read(db, paste.slug, password=None, is_owner=False)


def test_wrong_password_raises(db):
    paste = create(
        db, owner_id=_owner_id(), owner_name="x", body="secret", password="pass123", standalone=False
    )
    with pytest.raises(WrongPassword):
        load_for_read(db, paste.slug, password="wrong", is_owner=False)


def test_correct_password_serves(db):
    paste = create(
        db, owner_id=_owner_id(), owner_name="x", body="secret", password="pass123", standalone=False
    )
    result = load_for_read(db, paste.slug, password="pass123", is_owner=False)
    assert result.body == "secret"


def test_wrong_password_does_not_count_reads(db):
    paste = create(
        db, owner_id=_owner_id(), owner_name="x", body="hi", password="pw", read_limit=2, standalone=False
    )
    with pytest.raises(WrongPassword):
        load_for_read(db, paste.slug, password="wrong", is_owner=False)
    fresh = load_by_slug(db, paste.slug)
    assert fresh.read_count == 0


# ---------------------------------------------------------------------------
# delete


def test_delete_by_owner(db):
    owner = _owner_id()
    paste = create(db, owner_id=owner, owner_name="x", body="bye", standalone=False)
    delete_paste(db, paste.slug, owner_id=owner)
    with pytest.raises(Gone):
        load_for_read(db, paste.slug)


def test_delete_by_non_owner_denied(db):
    paste = create(db, owner_id=_owner_id(), owner_name="x", body="hi", standalone=False)
    with pytest.raises(Denied):
        delete_paste(db, paste.slug, owner_id=_owner_id())  # different id


def test_delete_by_admin(db):
    paste = create(db, owner_id=_owner_id(), owner_name="x", body="hi", standalone=False)
    delete_paste(db, paste.slug, owner_id=_owner_id(), is_admin=True)
    with pytest.raises(Gone):
        load_for_read(db, paste.slug)


# ---------------------------------------------------------------------------
# list


def test_list_own(db):
    owner = _owner_id()
    p1 = create(db, owner_id=owner, owner_name="x", body="one", standalone=False)
    p2 = create(db, owner_id=owner, owner_name="x", body="two", standalone=False)
    other = _owner_id()
    create(db, owner_id=other, owner_name="y", body="other", standalone=False)
    pastes = list_own(db, owner)
    slugs = {p.slug for p in pastes}
    assert p1.slug in slugs
    assert p2.slug in slugs
    assert len(pastes) == 2


def test_list_own_excludes_retired(db):
    owner = _owner_id()
    paste = create(db, owner_id=owner, owner_name="x", body="hi", standalone=False)
    delete_paste(db, paste.slug, owner_id=owner)
    assert list_own(db, owner) == []
    assert len(list_own(db, owner, include_retired=True)) == 1
