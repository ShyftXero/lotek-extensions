"""Evidence bytes in the CORE object store, and the four places that has to agree with itself.

The cutover is not one change, it is a WRITE side and three READ-shaped sides that must all use the
same reference:

* the two upload routes (cookie + machine) write either an ``object_id`` or a ``storage_path``;
* every byte reader has to resolve whichever it got;
* the report context builder has to hand the reader a reference rather than a raw path;
* every delete has to reach the store, or the blob outlives every row that pointed at it.

The first cut of this branch changed the BUILDERS and left three of the four readers on disk-only, and
nothing went red — the reports simply rendered without their evidence. So most of what is pinned here
is agreement between two places, not behaviour in one.
"""

from __future__ import annotations

import base64
import hashlib
import io
import pathlib
import uuid
from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from scribble.artifacts_storage import (
    MAX_OBJECT_BYTES,
    OBJECT_REF_PREFIX,
    artifact_ref,
    delete_file,
    object_id_of,
    read_object_bytes,
    save_bytes,
    store_bytes,
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


@dataclass
class _Blob:
    kind: str
    data: bytes
    content_type: str
    filename: str
    engagement_id: object
    deleted: bool = False


class FakeObjects:
    """Stands in for core's ``HostObjects`` and REFUSES what core refuses.

    A permissive fake is worse than none here: the whole class of bug this module exists for is
    scribble handing core something core rejects, so the fake enforces core's contract (exactly one of
    job_id/engagement_id, a real ``ObjectKind``, ``KeyError`` for anything not visible) rather than
    accepting whatever it is given.
    """

    def __init__(self) -> None:
        self.blobs: dict[uuid.UUID, _Blob] = {}
        self.puts: list[_Blob] = []
        self.refuse_put = False

    def put(self, actor, *, kind, stream, content_type, filename, job_id=None, engagement_id=None):
        if self.refuse_put:
            raise PermissionError("not an operator on the engagement")
        if (job_id is None) == (engagement_id is None):
            raise ValueError("exactly one of job_id / engagement_id is required")
        if kind not in CORE_OBJECT_KINDS:
            raise ValueError(f"invalid kind {kind!r}")
        data = stream.read()
        oid = uuid.uuid7()
        blob = _Blob(kind, data, content_type, filename, engagement_id)
        self.blobs[oid] = blob
        self.puts.append(blob)
        # No ``s3_key``: the ref an extension gets back never carries one.
        return SimpleNamespace(
            id=oid, kind=kind, byte_size=len(data), sha256=hashlib.sha256(data).hexdigest()
        )

    def open(self, actor, object_id):
        blob = self.blobs.get(object_id)
        if blob is None or blob.deleted:
            raise KeyError(object_id)  # absent and not-visible are one answer, as in core
        return io.BytesIO(blob.data)

    def stat(self, actor, object_id):
        blob = self.blobs.get(object_id)
        if blob is None or blob.deleted:
            return None
        return SimpleNamespace(id=object_id, byte_size=len(blob.data), kind=blob.kind)

    def delete(self, actor, object_id) -> bool:
        blob = self.blobs.get(object_id)
        if blob is None or blob.deleted:
            return False
        blob.deleted = True  # a tombstone, as in core — the GC reclaims the bytes later
        return True

    def list(self, actor, **_kw):
        return [SimpleNamespace(id=k) for k, v in self.blobs.items() if not v.deleted]


@pytest.fixture
def objects(app) -> FakeObjects:
    """Mount an object surface on the app, the way ``app/extensions.py::_inject_host`` does.

    Opt-in rather than part of ``stub_host``: a deployment with no object store is supported and is
    what every other artifact test in this suite exercises, so the default must stay "no store" or
    that fallback would silently stop being covered.
    """
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
        eng = Engagement(
            name="Q3",
            client_id=client_row.id,
            company_name="Acme Corp",
            core_engagement_id=uuid.uuid7(),
        )
        db.add(eng)
        db.commit()
        stub_host.viewable_client_ids = stub_host.viewable_client_ids | {client_row.id}
        return SimpleNamespace(
            id=eng.id, client_id=client_row.id, core_engagement_id=eng.core_engagement_id
        )


# --------------------------------------------------------------------------- store_bytes


def test_store_bytes_falls_back_to_disk_with_no_object_surface(app):
    """An operator running without SeaweedFS is a supported deployment, not a broken one."""
    with app.app_context():
        object_id, sha256, size = store_bytes(uuid.uuid7(), "shot.png", PNG)
    assert object_id is None
    assert sha256 == hashlib.sha256(PNG).hexdigest()
    assert size == len(PNG)


def test_store_bytes_falls_back_when_the_engagement_is_not_mapped_to_core(app, objects):
    """A standalone scribble engagement has no CORE engagement to authorize a put against."""
    with app.app_context():
        object_id, _sha, _size = store_bytes(None, "shot.png", PNG)
    assert object_id is None
    assert objects.puts == [], "nothing may be uploaded when there is nothing to authorize against"


def test_store_bytes_uses_a_kind_core_actually_accepts(app, objects, engagement):
    """The orphan-blob guard.

    ``kind="scribble_evidence"`` is not an ``ObjectKind`` member. Core validated the kind only after
    uploading, so the first evidence upload would have left a blob in the bucket with no row and no
    way to find it again. ``FakeObjects`` refuses a non-member the way core does, so a regression here
    is a ``ValueError`` out of ``put`` rather than a silent leak.
    """
    with app.app_context():
        object_id, sha256, size = store_bytes(
            engagement.core_engagement_id, "shot.png", PNG, content_type="image/png"
        )
    assert object_id is not None
    assert len(objects.puts) == 1
    put = objects.puts[0]
    assert put.kind in CORE_OBJECT_KINDS
    assert put.kind == "evidence"
    assert put.data == PNG
    assert put.engagement_id == engagement.core_engagement_id, "the CORE id, never scribble's own PK"
    assert sha256 == hashlib.sha256(PNG).hexdigest()
    assert size == len(PNG)


def test_store_bytes_does_not_turn_a_refused_upload_into_a_stored_one(app, objects, engagement):
    """A deny must propagate. Writing to disk on ``PermissionError`` would mean the store enforced the
    refusal and the fallback defeated it."""
    objects.refuse_put = True
    with app.app_context(), pytest.raises(PermissionError):
        store_bytes(engagement.core_engagement_id, "shot.png", PNG)


# --------------------------------------------------------------------------- the reference itself


def test_artifact_ref_prefers_the_object_id_and_falls_back_to_the_path():
    oid = uuid.uuid7()
    assert artifact_ref(SimpleNamespace(object_id=oid, storage_path="")) == f"{OBJECT_REF_PREFIX}{oid}"
    assert artifact_ref(SimpleNamespace(object_id=None, storage_path="7/x.png")) == "7/x.png"
    assert artifact_ref(SimpleNamespace(object_id=None, storage_path="")) == ""


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
    with app.app_context():
        object_id, _sha, _size = store_bytes(engagement.core_engagement_id, "shot.png", PNG)
        assert read_object_bytes(f"{OBJECT_REF_PREFIX}{object_id}", MAX_OBJECT_BYTES) == PNG


def test_read_object_bytes_refuses_an_oversized_blob_and_a_missing_one(app, objects, engagement):
    with app.app_context():
        object_id, _sha, _size = store_bytes(engagement.core_engagement_id, "big.bin", PNG)
        ref = f"{OBJECT_REF_PREFIX}{object_id}"
        # Exactly at the ceiling is fine; one byte under it refuses rather than truncating — a
        # truncated screenshot would render as corrupt evidence instead of as absent evidence.
        assert read_object_bytes(ref, len(PNG)) == PNG
        assert read_object_bytes(ref, len(PNG) - 1) is None
        assert read_object_bytes(f"{OBJECT_REF_PREFIX}{uuid.uuid7()}", MAX_OBJECT_BYTES) is None


def test_read_object_bytes_is_silent_with_no_object_surface(app):
    with app.app_context():
        assert read_object_bytes(f"{OBJECT_REF_PREFIX}{uuid.uuid7()}", MAX_OBJECT_BYTES) is None


# --------------------------------------------------------------------------- deleting


def test_delete_file_tombstones_a_store_backed_reference(app, objects, engagement):
    """The orphan guard on the DELETE side.

    Every delete in scribble funnels through ``delete_file``. Before this, all six call sites handed it
    a raw ``storage_path`` — empty for a store-backed row — so the DB row went and the blob stayed,
    with nothing left pointing at it.
    """
    with app.app_context():
        object_id, _sha, _size = store_bytes(engagement.core_engagement_id, "shot.png", PNG)
        # store_bytes hands back the id as a STRING (it goes into a ref); the store keys on the UUID.
        key = uuid.UUID(object_id)
        assert objects.blobs[key].deleted is False
        delete_file(app.extensions["scribble"], f"{OBJECT_REF_PREFIX}{object_id}")
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


def test_report_context_carries_the_object_reference_not_an_empty_path(
    app, objects, engagement, session_factory
):
    """The empty-gallery guard.

    ``_artifact_ctxs`` fed the renderers ``a.storage_path``, which is EMPTY for a store-backed row. The
    gallery then rendered nothing at all, in both renderers, with the whole suite green — the exact
    silent evidence loss this cutover is supposed to prevent.
    """
    object_id = uuid.uuid7()
    with session_factory() as db:
        group = FindingGroup(engagement_id=engagement.id, name="External", order_index=0)
        db.add(group)
        db.flush()
        finding = EngagementFinding(
            engagement_id=engagement.id, group_id=group.id, title="xss", severity=Severity.high
        )
        db.add(finding)
        db.flush()
        artifact = Artifact(
            engagement_id=engagement.id,
            finding_id=finding.id,
            kind=ArtifactKind.screenshot,
            placement=ArtifactPlacement.attached,
            filename="shot.png",
            content_type="image/png",
            storage_path="",
            object_id=object_id,
            byte_size=len(PNG),
            sha256=hashlib.sha256(PNG).hexdigest(),
            include_in_report=True,
        )
        db.add(artifact)
        db.commit()
        ctxs = _artifact_ctxs(list(finding.artifacts), engagement_id=engagement.id)

    assert len(ctxs) == 1
    assert ctxs[0].storage_path == f"{OBJECT_REF_PREFIX}{object_id}"


def test_every_artifact_byte_reader_resolves_an_object_reference():
    """The drift ratchet, and the reason this module exists.

    Three modules define the same ``_read(storage_path) -> bytes | None`` closure. The first cut of
    this branch taught ONE of them about ``obj:`` references while switching all the builders to emit
    them, so two renderers asked the filesystem for a path of the form ``obj:<uuid>``, got nothing, and
    dropped every image without a word.

    A per-reader test only ever covers the readers somebody remembered to write one for, so this finds
    them: any file defining that closure must resolve the prefix.
    """
    pkg = pathlib.Path(__file__).resolve().parent.parent / "scribble"
    blind = []
    readers = []
    for path in sorted(pkg.rglob("*.py")):
        src = path.read_text()
        start = src.find("def _read(storage_path")
        if start < 0:
            continue
        readers.append(path.name)
        # The BODY, not the module. Checking the whole file passes on nothing more than the import
        # line still being there — this test was written that way first, and neutralizing the branch
        # left it green, which is the same "guard that cannot fire" it is here to catch.
        end = src.find("return _read", start)
        body = src[start:end if end > start else len(src)]
        if "OBJECT_REF_PREFIX" not in body:
            blind.append(path.name)
    assert len(readers) >= 3, f"expected the known byte readers, found {readers}"
    assert not blind, (
        f"these modules read artifact bytes but cannot resolve an obj: reference: {blind} — a "
        f"store-backed artifact silently reads as absent through them"
    )


# --------------------------------------------------------------------------- end to end, over HTTP


def test_browser_upload_stores_in_the_object_store_and_downloads_back(
    client, app, objects, engagement, session_factory
):
    """The cookie surface, which is how a human actually uploads evidence.

    Only the machine route stored at first, so an ordinary browser upload kept writing to one host's
    disk while the feature was reported as shipped — and the download route, which resolves a path,
    answered "file missing on disk" for everything the machine route HAD stored.
    """
    resp = client.post(
        "/scribble/api/artifacts",
        json={
            "engagement_id": str(engagement.id),
            "filename": "shot.png",
            "content_base64": base64.b64encode(PNG).decode(),
        },
    )
    assert resp.status_code == 201, resp.get_json()
    artifact_id = resp.get_json()["id"]

    assert len(objects.puts) == 1, "a browser upload must reach the store, not the local filesystem"
    with session_factory() as db:
        row = db.get(Artifact, uuid.UUID(str(artifact_id)))
        assert row.object_id is not None
        assert row.storage_path == "", "a store-backed row keeps no disk path"

    raw = client.get(f"/scribble/api/artifacts/{artifact_id}/raw")
    assert raw.status_code == 200, raw.get_data(as_text=True)[:200]
    assert raw.data == PNG

    deleted = client.post(f"/scribble/api/artifacts/{artifact_id}/delete")
    assert deleted.status_code == 200
    assert all(b.deleted for b in objects.blobs.values()), "deleting the row must tombstone the blob"


def test_upload_still_uses_disk_when_the_host_has_no_object_store(
    client, app, stub_host, engagement, session_factory
):
    """No ``objects`` fixture here: the fallback deployment must keep working unchanged."""
    resp = client.post(
        "/scribble/api/artifacts",
        json={
            "engagement_id": str(engagement.id),
            "filename": "shot.png",
            "content_base64": base64.b64encode(PNG).decode(),
        },
    )
    assert resp.status_code == 201, resp.get_json()
    artifact_id = resp.get_json()["id"]
    with session_factory() as db:
        row = db.get(Artifact, uuid.UUID(str(artifact_id)))
        assert row.object_id is None
        assert row.storage_path, "with no store the bytes must still land on disk"

    raw = client.get(f"/scribble/api/artifacts/{artifact_id}/raw")
    assert raw.status_code == 200
    assert raw.data == PNG
