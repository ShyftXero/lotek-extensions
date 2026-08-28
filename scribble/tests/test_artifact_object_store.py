"""Evidence bytes in the CORE object store — ONE persist path, ONE reference, ONE reader.

The shape that matters here is not "can we talk to SeaweedFS". It is that nothing downstream has to
know where a given file went:

* `persist_bytes` is the only writer, so no route picks a backend and two routes cannot pick
  differently;
* `Artifact.storage_path` is the only column that answers "where are the bytes", so no reader has to
  decide which of two columns is authoritative;
* every reader resolves whichever kind of reference it is handed;
* `delete_file` is the only deleter, so a blob cannot outlive the row that pointed at it.

An earlier cut of this branch had two columns, two writers and four readers, only one of which knew
about store references. Nothing went red — the reports simply rendered without their evidence. Most
of what is pinned below is therefore agreement between two places, not behaviour in one.
"""

from __future__ import annotations

import base64
import dataclasses
import hashlib
import io
import pathlib
import uuid
from types import SimpleNamespace

import pytest

from scribble.artifacts_storage import (
    MAX_OBJECT_BYTES,
    OBJECT_REF_PREFIX,
    _acting_principal,
    delete_file,
    object_id_of,
    persist_bytes,
    read_object_bytes,
    save_bytes,
)
from scribble.enums import ArtifactKind, ArtifactPlacement, Severity
from scribble.models import Artifact, Client, Engagement, EngagementFinding, FindingGroup
from scribble.reporting.context import _artifact_ctxs

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32

#: Core's ``ObjectKind`` members, mirrored because an extension must not import a host module.
#: A kind outside this set is what core's ``ObjectStore.put`` refuses — and, before the core half of
#: this work was fixed, refused only AFTER uploading the bytes, leaving an unreferenced blob in the
#: bucket. Scribble passing a non-member was exactly that bug's trigger.
CORE_OBJECT_KINDS = frozenset({"artifact", "report", "screenshot", "evidence"})


# --------------------------------------------------------------------------- the host's object surface


@dataclasses.dataclass
class _Blob:
    kind: str
    data: bytes
    content_type: str
    filename: str
    engagement_id: object
    actor: object
    deleted: bool = False


class FakeObjects:
    """Stands in for core's ``HostObjects`` and REFUSES what core refuses.

    A permissive fake is worse than none. The bug class this module exists for is scribble handing
    core something core rejects, and the first version of this fake ignored its ``actor`` argument
    entirely — which let a change that would have raised ``PermissionError`` on every browser upload
    pass green. It now models core's contract: exactly one of job_id/engagement_id, a real
    ``ObjectKind``, a None principal holding zero engagements, and ``KeyError`` for anything not
    visible.
    """

    def __init__(self) -> None:
        self.blobs: dict[uuid.UUID, _Blob] = {}
        self.puts: list[_Blob] = []
        self.refuse_put = False

    def put(self, actor, *, kind, stream, content_type, filename, job_id=None, engagement_id=None):
        if actor is None or self.refuse_put:
            raise PermissionError("not an operator on the engagement")
        if (job_id is None) == (engagement_id is None):
            raise ValueError("exactly one of job_id / engagement_id is required")
        if kind not in CORE_OBJECT_KINDS:
            raise ValueError(f"invalid kind {kind!r}")
        data = stream.read()
        oid = uuid.uuid7()
        blob = _Blob(kind, data, content_type, filename, engagement_id, actor)
        self.blobs[oid] = blob
        self.puts.append(blob)
        # No ``s3_key``: the ref an extension gets back never carries one.
        return SimpleNamespace(
            id=oid, kind=kind, byte_size=len(data), sha256=hashlib.sha256(data).hexdigest()
        )

    def open(self, actor, object_id):
        blob = self.blobs.get(object_id)
        if actor is None or blob is None or blob.deleted:
            raise KeyError(object_id)  # absent and not-visible are one answer, as in core
        return io.BytesIO(blob.data)

    def stat(self, actor, object_id):
        blob = self.blobs.get(object_id)
        if actor is None or blob is None or blob.deleted:
            return None
        return SimpleNamespace(id=object_id, byte_size=len(blob.data), kind=blob.kind)

    def delete(self, actor, object_id) -> bool:
        blob = self.blobs.get(object_id)
        if actor is None or blob is None or blob.deleted:
            return False
        blob.deleted = True  # a tombstone, as in core — the GC reclaims the bytes later
        return True

    def list(self, actor, **_kw):
        if actor is None:
            return []
        return [SimpleNamespace(id=k) for k, v in self.blobs.items() if not v.deleted]


@pytest.fixture
def objects(app) -> FakeObjects:
    """Mount an object surface, the way ``app/extensions.py::_inject_host`` does."""
    surface = FakeObjects()
    app.extensions["scribble"].extras["objects"] = surface
    return surface


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


@pytest.fixture
def unmapped_engagement(app, stub_host, session_factory):
    """A scribble engagement with NO core engagement behind it — the standalone-report case."""
    with session_factory() as db:
        client_row = Client(name="Beta")
        db.add(client_row)
        db.flush()
        eng = Engagement(name="Q4", client_id=client_row.id, company_name="Beta Corp")
        db.add(eng)
        db.commit()
        stub_host.viewable_client_ids = stub_host.viewable_client_ids | {client_row.id}
        return SimpleNamespace(id=eng.id, client_id=client_row.id, core_engagement_id=None)


def _persist(app, cfg, engagement, **kw):
    with app.app_context():
        return persist_bytes(cfg, engagement_id=engagement.id,
                             core_engagement_id=engagement.core_engagement_id,
                             filename="shot.png", data=PNG, **kw)


# --------------------------------------------------------------------------- one column


def test_the_artifact_row_has_exactly_one_place_for_the_reference():
    """The ratchet against reintroducing the split.

    A second column (`object_id`) beside `storage_path` means every reader must decide which one is
    authoritative for a given row. Five call sites had that choice; two of them chose wrong and the
    evidence gallery rendered empty in both report renderers with the suite green.
    """
    columns = {c.name for c in Artifact.__table__.columns}
    assert "storage_path" in columns
    assert "object_id" not in columns, (
        "two columns for one fact — `storage_path` already holds either an obj: reference or a disk "
        "path, and a second column just gives readers something to disagree about"
    )


# --------------------------------------------------------------------------- persist_bytes


def test_persist_bytes_stores_in_the_object_store(app, objects, engagement):
    cfg = app.extensions["scribble"]
    ref, sha256, size = _persist(app, cfg, engagement, content_type="image/png")

    assert ref.startswith(OBJECT_REF_PREFIX)
    assert len(objects.puts) == 1
    put = objects.puts[0]
    assert put.kind in CORE_OBJECT_KINDS, "a kind core would refuse orphans the blob it just uploaded"
    assert put.kind == "evidence"
    assert put.data == PNG
    assert put.engagement_id == engagement.core_engagement_id, "the CORE id, never scribble's own PK"
    assert sha256 == hashlib.sha256(PNG).hexdigest()
    assert size == len(PNG)


def test_persist_bytes_uses_disk_when_there_is_no_host_object_surface(app, engagement):
    """Standalone scribble has no host at all — disk is the only place the bytes can go."""
    cfg = app.extensions["scribble"]
    ref, _sha, _size = _persist(app, cfg, engagement)
    assert not ref.startswith(OBJECT_REF_PREFIX)
    assert (pathlib.Path(cfg.artifact_root) / ref).is_file()


def test_persist_bytes_uses_disk_for_an_engagement_with_no_core_mapping(
    app, objects, unmapped_engagement
):
    """Core files every blob under a core engagement — INV-OBJSTORE-01 makes that a database fact via
    composite FKs — so an unmapped scribble engagement has nowhere in the bucket to put one."""
    cfg = app.extensions["scribble"]
    ref, _sha, _size = _persist(app, cfg, unmapped_engagement)
    assert not ref.startswith(OBJECT_REF_PREFIX)
    assert objects.puts == [], "nothing may be uploaded when there is nothing to authorize against"


def test_persist_bytes_does_not_turn_a_refused_upload_into_a_disk_write(app, objects, engagement):
    """A deny must propagate. Falling back to disk would mean the store enforced the refusal and the
    fallback defeated it — and the operator would never learn the evidence went somewhere else."""
    objects.refuse_put = True
    cfg = app.extensions["scribble"]
    with pytest.raises(PermissionError):
        _persist(app, cfg, engagement)
    root = pathlib.Path(cfg.artifact_root)
    assert not [p for p in root.rglob("*") if p.is_file()], "no bytes may reach disk on a refusal"


def test_the_acting_principal_falls_back_to_the_session_user(app, objects, engagement, stub_host):
    """The change that let the BROWSER surface use the store at all.

    ``host_contract.pat_actor()`` reads ``g.api_user_id``, which only PAT authentication sets, so on a
    cookie request it is None. Reading only that hook is why the object store was reachable from
    machine routes only, and why the browser surface kept its own parallel filesystem.
    """
    stub_host.actor = None  # a browser request: no PAT principal
    with app.app_context():
        assert _acting_principal() is not None, "a logged-in session user is still a principal"

    cfg = app.extensions["scribble"]
    ref, _sha, _size = _persist(app, cfg, engagement)
    assert ref.startswith(OBJECT_REF_PREFIX), "a browser upload must reach the store"
    assert objects.puts[0].actor is not None


# --------------------------------------------------------------------------- the reference


def test_object_id_of_round_trips_and_refuses_anything_else():
    oid = uuid.uuid7()
    assert object_id_of(f"{OBJECT_REF_PREFIX}{oid}") == oid
    assert object_id_of("7/shot.png") is None, "a disk path is not a store reference"
    assert object_id_of("") is None
    assert object_id_of(f"{OBJECT_REF_PREFIX}not-a-uuid") is None
    # The prefix is the ONLY thing that routes a read at the store, so a path that merely contains it
    # must not be mistaken for one.
    assert object_id_of(f"7/{OBJECT_REF_PREFIX}{oid}") is None


# --------------------------------------------------------------------------- reading back


def test_read_object_bytes_returns_the_blob(app, objects, engagement):
    cfg = app.extensions["scribble"]
    ref, _sha, _size = _persist(app, cfg, engagement)
    with app.app_context():
        assert read_object_bytes(ref, MAX_OBJECT_BYTES) == PNG


def test_read_object_bytes_refuses_an_oversized_blob_and_a_missing_one(app, objects, engagement):
    cfg = app.extensions["scribble"]
    ref, _sha, _size = _persist(app, cfg, engagement)
    with app.app_context():
        # Exactly at the ceiling is fine; one byte under it refuses rather than truncating — a
        # truncated screenshot renders as corrupt evidence instead of as absent evidence.
        assert read_object_bytes(ref, len(PNG)) == PNG
        assert read_object_bytes(ref, len(PNG) - 1) is None
        assert read_object_bytes(f"{OBJECT_REF_PREFIX}{uuid.uuid7()}", MAX_OBJECT_BYTES) is None


def test_read_object_bytes_is_silent_with_no_object_surface(app):
    with app.app_context():
        assert read_object_bytes(f"{OBJECT_REF_PREFIX}{uuid.uuid7()}", MAX_OBJECT_BYTES) is None


# --------------------------------------------------------------------------- deleting


def test_delete_file_tombstones_a_store_backed_reference(app, objects, engagement):
    """The orphan guard on the DELETE side.

    Every delete in scribble funnels through ``delete_file`` — artifact, finding and engagement
    delete, on both surfaces. Before the store branch lived here, a store-backed row dropped its DB
    row and left the blob in the bucket with nothing pointing at it.
    """
    cfg = app.extensions["scribble"]
    ref, _sha, _size = _persist(app, cfg, engagement)
    key = object_id_of(ref)
    assert objects.blobs[key].deleted is False
    with app.app_context():
        delete_file(cfg, ref)
    assert objects.blobs[key].deleted is True


def test_delete_file_still_unlinks_a_disk_path(app):
    cfg = app.extensions["scribble"]
    storage_path, _sha, _size = save_bytes(cfg, 7, "shot.png", PNG)
    on_disk = pathlib.Path(cfg.artifact_root) / storage_path
    assert on_disk.is_file()
    delete_file(cfg, storage_path)
    assert not on_disk.exists()


def test_delete_file_swallows_a_store_that_cannot_delete(app, objects):
    """Best-effort: the DB rows are already gone by the time this runs, so a raising store would 500 a
    request that had otherwise succeeded."""

    def _boom(_actor, _object_id):
        raise RuntimeError("object store is not configured")

    objects.delete = _boom
    with app.app_context():
        delete_file(app.extensions["scribble"], f"{OBJECT_REF_PREFIX}{uuid.uuid7()}")


# --------------------------------------------------------------------------- the report context


def test_report_context_carries_the_reference_through_verbatim(
    app, objects, engagement, session_factory
):
    """The empty-gallery guard.

    With two columns this had to pick one, and picked the empty one — so the gallery rendered nothing
    in both renderers with the suite green. With one column it is a plain copy, which is the point:
    there is no longer a choice to get wrong.
    """
    cfg = app.extensions["scribble"]
    ref, sha256, size = _persist(app, cfg, engagement)
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
    assert ctxs[0].storage_path.startswith(OBJECT_REF_PREFIX)


def test_every_artifact_byte_reader_resolves_an_object_reference():
    """The drift ratchet, and the reason this module exists.

    Three modules define the same ``_read(storage_path) -> bytes | None`` closure. One of them learned
    about ``obj:`` references while every builder was switched to emit them, so two renderers asked
    the filesystem for a path spelled ``obj:<uuid>``, got nothing, and dropped every image without a
    word.

    A per-reader test only ever covers the readers somebody remembered to write one for, so this finds
    them all: any file defining that closure must resolve the prefix IN THE CLOSURE BODY. Checking the
    whole file instead passes on the import line alone — this test was written that way first, and
    neutralizing the branch left it green.
    """
    pkg = pathlib.Path(__file__).resolve().parent.parent / "scribble"
    blind, readers = [], []
    for path in sorted(pkg.rglob("*.py")):
        src = path.read_text()
        start = src.find("def _read(storage_path")
        if start < 0:
            continue
        readers.append(path.name)
        end = src.find("return _read", start)
        if "OBJECT_REF_PREFIX" not in src[start:end if end > start else len(src)]:
            blind.append(path.name)
    assert len(readers) >= 3, f"expected the known byte readers, found {readers}"
    assert not blind, (
        f"these modules read artifact bytes but cannot resolve an obj: reference: {blind} — a "
        f"store-backed artifact silently reads as absent through them"
    )


# --------------------------------------------------------------------------- end to end, over HTTP


def test_browser_upload_stores_and_downloads_back(client, app, objects, engagement, session_factory):
    """The cookie surface — how a human actually attaches evidence — end to end.

    This route used to write to disk directly, so which backend held a piece of evidence depended on
    which route a human happened to use.
    """
    resp = client.post("/scribble/api/artifacts", json={
        "engagement_id": str(engagement.id), "filename": "shot.png",
        "content_base64": base64.b64encode(PNG).decode(),
    })
    assert resp.status_code == 201, resp.get_json()
    artifact_id = resp.get_json()["id"]

    assert len(objects.puts) == 1, "a browser upload must reach the store, not the local filesystem"
    with session_factory() as db:
        row = db.get(Artifact, uuid.UUID(str(artifact_id)))
        assert row.storage_path.startswith(OBJECT_REF_PREFIX)

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
    """Not a 500, and not a silent disk write."""
    objects.refuse_put = True
    if surface == "cookie":
        resp = client.post("/scribble/api/artifacts", json={
            "engagement_id": str(engagement.id), "filename": "shot.png",
            "content_base64": base64.b64encode(PNG).decode(),
        })
    else:
        resp = client.post(
            f"/scribble/machine/engagements/{engagement.id}/artifacts",
            json={"filename": "shot.png", "content_base64": base64.b64encode(PNG).decode()},
        )
    assert resp.status_code == 403, (resp.status_code, resp.get_data(as_text=True)[:200])
