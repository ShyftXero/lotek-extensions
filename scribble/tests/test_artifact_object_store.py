"""Evidence lives in the object store. Only there, by one path, under one reference.

The property, stated once: **nothing scribble persists touches a filesystem.** Not standalone, not on
the browser surface, not on the machine surface. The disk arm is gone rather than narrowed, because a
narrowed one is still a second answer to "where are the bytes" — and that difference is what produced
the bugs this branch spent its length fixing (an evidence gallery that rendered empty while the whole
suite stayed green).

What holds it up:

* `persist_bytes` is the only writer — no route picks a backend, so two routes cannot disagree;
* `Artifact.storage_path` is the only column that answers where — no reader has to choose;
* `artifact_bytes` is the only reader — three near-identical closures used to exist and only one of
  them learned about store references;
* `delete_file` is the only deleter — a blob cannot outlive the row that pointed at it;
* a mounted scribble engagement gets a CORE engagement at create time, because
  `objects.engagement_id` is NOT NULL for every blob (INV-OBJSTORE-01 makes tenancy a database fact
  via composite FKs) and an unanchored engagement has nowhere in the bucket to go.
"""

from __future__ import annotations

import base64
import hashlib
import pathlib
import uuid
from types import SimpleNamespace

import pytest

from scribble.artifacts_storage import (
    OBJECT_REF_PREFIX,
    _acting_principal,
    artifact_bytes,
    delete_file,
    object_id_of,
    persist_bytes,
    read_object_bytes,
)
from scribble.enums import ArtifactKind, ArtifactPlacement, Severity
from scribble.models import Artifact, Client, Engagement, EngagementFinding, FindingGroup
from scribble.reporting.context import _artifact_ctxs
from scribble.testing import CORE_OBJECT_KINDS, InMemoryObjects, MockCoreEngagements, wire_mock_host

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


@pytest.fixture
def objects(app) -> InMemoryObjects:
    """The mock host's object surface for THIS app.

    `scribble.testing.InMemoryObjects` rather than a fake local to this file: a second fake is a
    second thing that can drift from what core actually refuses, and an earlier local one ignored its
    `actor` argument entirely — which let a change that would have raised `PermissionError` on every
    browser upload in production pass green.
    """
    surface, _engagements = wire_mock_host(app.extensions["scribble"])
    return surface


@pytest.fixture
def engagements(app) -> MockCoreEngagements:
    surface = InMemoryObjects()
    _s, eng = wire_mock_host(app.extensions["scribble"], objects=surface)
    return eng


@pytest.fixture
def engagement(app, stub_host, session_factory):
    """A scribble engagement MAPPED to a core engagement, visible to the stub actor."""
    with session_factory() as db:
        client_row = Client(name="Acme")
        db.add(client_row)
        db.flush()
        eng = Engagement(name="Q3", client_id=client_row.id, company_name="Acme Corp",
                         core_engagement_id=uuid.uuid7())
        db.add(eng)
        db.commit()
        stub_host.viewable_client_ids = stub_host.viewable_client_ids | {client_row.id}
        return SimpleNamespace(id=eng.id, client_id=client_row.id,
                               core_engagement_id=eng.core_engagement_id)


def _persist(app, engagement, **kw):
    with app.app_context():
        return persist_bytes(core_engagement_id=engagement.core_engagement_id,
                             filename="shot.png", data=PNG, **kw)


# --------------------------------------------------------------------------- nothing touches disk


def test_the_package_has_no_way_left_to_write_a_file(app):
    """The ratchet on the whole point.

    A local-disk writer is not a smaller version of this feature, it is the return of the split. The
    helpers that used to provide one (`save_bytes`, `resolve_path`, `safe_join`) are deleted, and this
    fails if any of them comes back — including under a new name that still writes bytes to a path.
    """
    import scribble.artifacts_storage as storage

    for gone in ("save_bytes", "resolve_path", "safe_join", "_bounded_name"):
        assert not hasattr(storage, gone), (
            f"{gone} is back: evidence has one home, and a disk writer beside it is the difference "
            f"this cutover removed"
        )
    src = pathlib.Path(storage.__file__).read_text()
    # Filesystem-SPECIFIC markers only. An earlier version of this scanned for "open(" and matched
    # `surface.open(...)` — the store's own streaming read — so the guard failed on correct code. A
    # check that cries wolf gets deleted by the next person, which is worse than not having it.
    for hazard in ("pathlib", "write_bytes", "read_bytes", ".unlink(", "mkdir", "os.path"):
        assert hazard not in src, f"artifacts_storage touches the filesystem again: {hazard!r}"


def test_the_artifact_row_has_exactly_one_place_for_the_reference():
    """A second column (`object_id`) beside `storage_path` means every reader must decide which one is
    authoritative. Five call sites had that choice; two chose wrong and both renderers went blank."""
    columns = {c.name for c in Artifact.__table__.columns}
    assert "storage_path" in columns
    assert "object_id" not in columns, "two columns for one fact is something for readers to disagree on"


# --------------------------------------------------------------------------- persist_bytes


def test_persist_bytes_stores_in_the_object_store(app, objects, engagement):
    ref, sha256, size = _persist(app, engagement, content_type="image/png")

    assert ref.startswith(OBJECT_REF_PREFIX)
    assert len(objects.puts) == 1
    put = objects.puts[0]
    assert put.kind in CORE_OBJECT_KINDS, "a kind core would refuse orphans the blob it just uploaded"
    assert put.kind == "evidence"
    assert put.data == PNG
    assert put.engagement_id == engagement.core_engagement_id, "the CORE id, never scribble's own PK"
    assert sha256 == hashlib.sha256(PNG).hexdigest()
    assert size == len(PNG)


def test_persist_bytes_refuses_when_the_host_has_no_object_store(app, engagement):
    """No silent disk fallback — not even here.

    Standalone Scribble is a TESTBED, not a deployment: the shell that boots it supplies a mock host
    (`scribble.testing.wire_mock_host`). Falling back to disk "just for standalone" is what put
    evidence in two places, so this is a loud refusal naming the fix.
    """
    app.extensions["scribble"].extras.pop("objects", None)
    with pytest.raises(RuntimeError, match="wire_mock_host"):
        _persist(app, engagement)


def test_persist_bytes_refuses_an_engagement_with_no_core_anchor(app, objects):
    """`objects.engagement_id` is NOT NULL for every blob, so there is nowhere to file this.

    It should be unreachable — a mounted engagement is given a core engagement at CREATE time — which
    is exactly why it raises rather than degrading: if it ever happens, the create path has a hole and
    a disk write would hide it.
    """
    unmapped = SimpleNamespace(core_engagement_id=None)
    with pytest.raises(RuntimeError, match="no core_engagement_id"):
        _persist(app, unmapped)
    assert objects.puts == []


def test_persist_bytes_does_not_turn_a_refused_upload_into_a_stored_one(app, objects, engagement):
    """A deny must propagate — there is no longer anywhere for it to be quietly written instead."""
    objects.refuse_put = True
    with pytest.raises(PermissionError):
        _persist(app, engagement)
    assert objects.blobs == {}


def test_the_mock_host_refuses_a_none_principal_like_core_does():
    """The fake's refusals are load-bearing, so they get their own guard.

    `test_the_acting_principal_falls_back_to_the_session_user` only has teeth because the surface
    REFUSES a None actor — make the fake permissive and that test passes with the fallback deleted.
    An earlier local fake ignored `actor` entirely, and a change that would have raised
    `PermissionError` on every browser upload in production went green on it.
    """
    import io as _io

    surface = InMemoryObjects()
    with pytest.raises(PermissionError):
        surface.put(None, kind="evidence", stream=_io.BytesIO(b"x"), content_type="image/png",
                    filename="s.png", engagement_id=uuid.uuid7())
    with pytest.raises(KeyError):
        surface.open(None, uuid.uuid7())
    assert surface.delete(None, uuid.uuid7()) is False
    assert surface.stat(None, uuid.uuid7()) is None
    assert surface.list(None) == []


def test_the_acting_principal_falls_back_to_the_session_user(app, objects, engagement, stub_host):
    """The change that let the BROWSER surface use the store at all.

    `host_contract.pat_actor()` reads `g.api_user_id`, which only PAT authentication sets, so on a
    cookie request it is None. Reading only that hook is why the object store was reachable from
    machine routes only, and why the browser surface kept a parallel filesystem.
    """
    stub_host.actor = None  # a browser request: no PAT principal
    with app.app_context():
        assert _acting_principal() is not None, "a logged-in session user is still a principal"
    assert _persist(app, engagement)[0].startswith(OBJECT_REF_PREFIX)


# --------------------------------------------------------------------------- the reference


def test_object_id_of_round_trips_and_refuses_anything_else():
    oid = uuid.uuid7()
    assert object_id_of(f"{OBJECT_REF_PREFIX}{oid}") == oid
    assert object_id_of("7/shot.png") is None, "a legacy disk path is not a store reference"
    assert object_id_of("") is None
    assert object_id_of(f"{OBJECT_REF_PREFIX}not-a-uuid") is None
    # The prefix is the ONLY thing that routes a read at the store, so a value that merely contains it
    # must not be mistaken for one.
    assert object_id_of(f"7/{OBJECT_REF_PREFIX}{oid}") is None


# --------------------------------------------------------------------------- reading back


def test_artifact_bytes_is_the_one_reader(app, objects, engagement):
    ref, _sha, _size = _persist(app, engagement)
    with app.app_context():
        assert artifact_bytes(ref) == PNG
        assert artifact_bytes(f"{OBJECT_REF_PREFIX}{uuid.uuid7()}") is None
        assert artifact_bytes("7/legacy-on-disk.png") is None, "a pre-cutover row reads as ABSENT"


def test_read_object_bytes_refuses_an_oversized_blob(app, objects, engagement):
    ref, _sha, _size = _persist(app, engagement)
    with app.app_context():
        # Exactly at the ceiling is fine; one byte under refuses rather than truncating — a truncated
        # screenshot renders as corrupt evidence instead of as absent evidence.
        assert read_object_bytes(ref, len(PNG)) == PNG
        assert read_object_bytes(ref, len(PNG) - 1) is None


def test_reading_is_silent_with_no_object_surface(app):
    app.extensions["scribble"].extras.pop("objects", None)
    with app.app_context():
        assert artifact_bytes(f"{OBJECT_REF_PREFIX}{uuid.uuid7()}") is None


# --------------------------------------------------------------------------- deleting


def test_delete_file_tombstones_the_blob(app, objects, engagement):
    """Every delete in scribble funnels through here — artifact, finding and engagement delete, on
    both surfaces. Before that, a row's bytes stayed in the bucket with nothing pointing at them."""
    ref, _sha, _size = _persist(app, engagement)
    key = object_id_of(ref)
    assert objects.blobs[key].deleted is False
    with app.app_context():
        delete_file(ref)
    assert objects.blobs[key].deleted is True


def test_delete_file_swallows_a_store_that_cannot_delete(app, objects):
    """Best-effort: the DB rows are already gone by the time this runs, so a raising store would 500 a
    request that had otherwise succeeded."""

    def _boom(_actor, _object_id):
        raise RuntimeError("object store is not configured")

    objects.delete = _boom
    with app.app_context():
        delete_file(f"{OBJECT_REF_PREFIX}{uuid.uuid7()}")


# --------------------------------------------------------------------------- the report context


def test_report_context_carries_the_reference_through_verbatim(
    app, objects, engagement, session_factory
):
    """With two columns this had to pick one and picked the empty one, so the gallery rendered nothing
    in both renderers with the suite green. With one column it is a plain copy — no choice to fumble."""
    ref, sha256, size = _persist(app, engagement)
    with session_factory() as db:
        group = FindingGroup(engagement_id=engagement.id, name="External", order_index=0)
        db.add(group)
        db.flush()
        finding = EngagementFinding(engagement_id=engagement.id, group_id=group.id, title="xss",
                                    severity=Severity.high)
        db.add(finding)
        db.flush()
        db.add(Artifact(
            engagement_id=engagement.id, finding_id=finding.id, kind=ArtifactKind.screenshot,
            placement=ArtifactPlacement.attached, filename="shot.png", content_type="image/png",
            storage_path=ref, byte_size=size, sha256=sha256, include_in_report=True,
        ))
        db.commit()
        ctxs = _artifact_ctxs(list(finding.artifacts), engagement_id=engagement.id)

    assert len(ctxs) == 1
    assert ctxs[0].storage_path == ref


def test_there_is_exactly_one_artifact_byte_reader():
    """The drift ratchet.

    Three modules used to define the same `_read(storage_path)` closure. One learned about `obj:`
    references while every builder was switched to emit them, so two renderers asked the filesystem
    for a path spelled `obj:<uuid>` and dropped every image without a word. They are one function now,
    and this fails if a second one reappears — the shape of the original defect, not the defect.
    """
    pkg = pathlib.Path(__file__).resolve().parent.parent / "scribble"
    duplicates = [p.name for p in sorted(pkg.rglob("*.py")) if "def _read(storage_path" in p.read_text()]
    assert not duplicates, (
        f"a second artifact byte reader appeared in {duplicates} — use artifacts_storage."
        f"artifact_bytes, or the two will disagree about what a reference means"
    )


# --------------------------------------------------------------------------- end to end, over HTTP


def _upload(client, engagement_id):
    return client.post("/scribble/api/artifacts", json={
        "engagement_id": str(engagement_id), "filename": "shot.png",
        "content_base64": base64.b64encode(PNG).decode(),
    })


def test_browser_upload_stores_and_downloads_back(client, app, objects, engagement, session_factory):
    """The cookie surface — how a human actually attaches evidence — end to end."""
    resp = _upload(client, engagement.id)
    assert resp.status_code == 201, resp.get_json()
    artifact_id = resp.get_json()["id"]

    assert len(objects.puts) == 1, "a browser upload must reach the store, not a local filesystem"
    with session_factory() as db:
        assert db.get(Artifact, uuid.UUID(str(artifact_id))).storage_path.startswith(OBJECT_REF_PREFIX)

    raw = client.get(f"/scribble/api/artifacts/{artifact_id}/raw")
    assert raw.status_code == 200, raw.get_data(as_text=True)[:200]
    assert raw.data == PNG

    deleted = client.post(f"/scribble/api/artifacts/{artifact_id}/delete")
    assert deleted.status_code == 200
    assert all(b.deleted for b in objects.blobs.values()), "deleting the row must tombstone the blob"


def test_machine_upload_stores_through_the_same_path(client, objects, engagement, session_factory):
    """Both surfaces, one backend — the property that stops evidence splitting by route."""
    resp = client.post(
        f"/scribble/machine/engagements/{engagement.id}/artifacts",
        json={"filename": "shot.png", "content_base64": base64.b64encode(PNG).decode()},
    )
    assert resp.status_code == 201, resp.get_data(as_text=True)[:200]
    assert len(objects.puts) == 1
    with session_factory() as db:
        row = db.get(Artifact, uuid.UUID(str(resp.get_json()["id"])))
        assert row.storage_path.startswith(OBJECT_REF_PREFIX)


@pytest.mark.parametrize("surface", ["cookie", "machine"])
def test_a_refused_put_is_403_on_both_surfaces(client, objects, engagement, surface):
    """Not a 500, and — now that there is nowhere else — not a quiet disk write either."""
    objects.refuse_put = True
    if surface == "cookie":
        resp = _upload(client, engagement.id)
    else:
        resp = client.post(
            f"/scribble/machine/engagements/{engagement.id}/artifacts",
            json={"filename": "shot.png", "content_base64": base64.b64encode(PNG).decode()},
        )
    assert resp.status_code == 403, (resp.status_code, resp.get_data(as_text=True)[:200])


# --------------------------------------------------------------------------- the anchor, at create


def test_creating_an_engagement_asks_the_host_for_a_core_engagement(
    client, app, stub_host, engagements, session_factory
):
    """Where the anchor comes from. Obtained at CREATE time so it is never missing at upload time."""
    acme = uuid.uuid7()
    stub_host.viewable_client_ids = stub_host.viewable_client_ids | {acme}
    resp = client.post("/scribble/machine/engagements", json={"name": "Q4", "client_id": str(acme)})
    assert resp.status_code == 201, resp.get_data(as_text=True)[:300]
    assert engagements.created == [(acme, "Q4")]

    body = resp.get_json()
    assert body["core_engagement_id"] is not None
    with session_factory() as db:
        assert db.get(Engagement, uuid.UUID(str(body["id"]))).core_engagement_id is not None


def test_a_caller_that_cannot_create_one_gets_403_not_an_unusable_engagement(
    client, app, stub_host, engagements
):
    """Creating an engagement is manager-or-admin in the host, and the seam delegates to core's own
    rule rather than restating it. The honest answer is a refusal — NOT a scribble engagement whose
    evidence would have nowhere to go, which is the failure the anchor exists to prevent.
    """
    engagements.allow_create = False
    acme = uuid.uuid7()
    stub_host.viewable_client_ids = stub_host.viewable_client_ids | {acme}
    resp = client.post("/scribble/machine/engagements", json={"name": "Q4", "client_id": str(acme)})
    assert resp.status_code == 403, (resp.status_code, resp.get_data(as_text=True)[:200])


def test_a_caller_may_supply_a_core_engagement_it_already_operates(
    client, app, stub_host, engagements, session_factory
):
    """Creating one is privileged; pointing at one you already operate is not. Without this arm every
    plain operator would be locked out of filing evidence."""
    acme, existing = uuid.uuid7(), uuid.uuid7()
    stub_host.viewable_client_ids = stub_host.viewable_client_ids | {acme}
    app.extensions["scribble"].extras["can_operate_on"] = lambda _eid: True
    engagements.allow_create = False  # it must NOT need to create one

    resp = client.post("/scribble/machine/engagements", json={
        "name": "Q4", "client_id": str(acme), "core_engagement_id": str(existing)})
    assert resp.status_code == 201, resp.get_data(as_text=True)[:300]
    assert engagements.created == []
    with session_factory() as db:
        stored = db.get(Engagement, uuid.UUID(str(resp.get_json()["id"]))).core_engagement_id
    assert str(stored) == str(existing)


def test_a_supplied_core_engagement_the_caller_cannot_operate_is_refused(
    client, app, stub_host, engagements
):
    """Otherwise 'supply your own' would be a way to file evidence into somebody else's engagement."""
    acme = uuid.uuid7()
    stub_host.viewable_client_ids = stub_host.viewable_client_ids | {acme}
    app.extensions["scribble"].extras["can_operate_on"] = lambda _eid: False

    resp = client.post("/scribble/machine/engagements", json={
        "name": "Q4", "client_id": str(acme), "core_engagement_id": str(uuid.uuid7())})
    assert resp.status_code == 404, (resp.status_code, resp.get_data(as_text=True)[:200])
