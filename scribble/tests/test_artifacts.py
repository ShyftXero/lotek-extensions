"""WS5 tests: artifact storage helpers + the artifacts API blueprint hook.

Self-contained: builds its own Flask app (overriding the module-scoped ``app`` fixture from
tests/conftest.py) so it can wire ``scribble.artifacts_api.register(api_bp, bp)`` onto the *shared*
blueprint singletons before ``scribble.register()`` attaches them to the app — mirroring exactly what
the driver will eventually do inside ``scribble/__init__.py``. ``register()`` is idempotent, so this is
safe even though it runs once per test app in the same pytest process.
"""

from __future__ import annotations

import io
import uuid

import pytest
from flask import Flask
from sqlalchemy import create_engine

import scribble
from scribble.api import api_bp
from scribble.artifacts_api import artifact_url
from scribble.artifacts_api import register as register_artifacts
from scribble.artifacts_storage import (
    SAFE_NAME_MAX,
    guess_content_type,
    resolve_path,
    safe_join,
    save_bytes,
)
from scribble.blueprint import bp
from scribble.enums import ArtifactPlacement, Severity
from scribble.models import Artifact, Client, Engagement, EngagementFinding, FindingGroup
from scribble.reporting import build_report_context
from scribble.seed import seed_defaults

_MISSING_ID = uuid.uuid7()  # a well-formed id that is not in the table

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


@pytest.fixture
def app(tmp_path):
    # Wires the WS5 hook onto the shared blueprint singletons *before* scribble.register() attaches
    # them to this app. Guarded by an idempotency flag in artifacts_api.register, so repeating this in
    # every test in this module (and it having already run in earlier tests) is safe.
    register_artifacts(api_bp, bp)

    flask_app = Flask(__name__)
    flask_app.config["SECRET_KEY"] = "test"
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}", future=True)
    cfg = scribble.register(
        flask_app, engine, instance_path=str(tmp_path), base_template="scribble/base.html"
    )
    with cfg.session_factory() as session:
        seed_defaults(session)
        session.commit()
    return flask_app


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def session_factory(app):
    return app.extensions["scribble"].session_factory


@pytest.fixture
def cfg(app):
    return app.extensions["scribble"]


def _make_engagement(db, *, client_name: str = "Acme") -> Engagement:
    c = Client(name=client_name)
    db.add(c)
    db.flush()
    eng = Engagement(name="Q3", client_id=c.id, company_name=f"{client_name} Corp")
    db.add(eng)
    db.commit()
    return eng


def _make_finding(db, engagement) -> EngagementFinding:
    group = FindingGroup(engagement=engagement, name="External", order_index=0)
    finding = EngagementFinding(engagement=engagement, group=group, title="xss", severity=Severity.high)
    db.add(group)
    db.add(finding)
    db.commit()
    return finding


# --------------------------------------------------------------------------- register() hook


def test_register_is_idempotent():
    # Calling register() again (as the fixture above already did at collection time for this module)
    # must not raise, and must not duplicate routes on repeated app registration.
    register_artifacts(api_bp, bp)
    register_artifacts(api_bp, bp)


def test_health_still_works_after_artifacts_registered(client):
    # Sanity: adding artifact routes doesn't disturb Sprint-0 routes on the shared blueprints.
    resp = client.get("/scribble/api/health")
    assert resp.status_code == 200


# --------------------------------------------------------------------------- storage helpers


def test_save_bytes_writes_file_and_returns_metadata(cfg):
    storage_path, sha256, byte_size = save_bytes(cfg, 1, "shot.png", PNG_BYTES)
    assert byte_size == len(PNG_BYTES)
    assert len(sha256) == 64
    resolved = resolve_path(cfg, storage_path)
    assert resolved.is_file()
    assert resolved.read_bytes() == PNG_BYTES
    # storage_path is relative and namespaced under the engagement id.
    assert storage_path.startswith("1/")


def test_save_bytes_bounds_name_after_secure_filename(cfg):
    """#55 residual 1: ``secure_filename`` NFKD-normalizes and can EXPAND, not just shrink, the
    caller's filename (``len(secure_filename('½' * 222)) == 444``, measured against the installed
    werkzeug). An unbounded ``safe_name`` built from that would overrun ``NAME_MAX`` (255) once
    ``save_bytes`` prefixes ``"<uuid4hex>_"`` and raise ``OSError: [Errno 36] File name too long`` --
    a 500 on what looks, at the API boundary, like a reasonably-sized name. Pins that ``save_bytes``
    truncates the SECURED name (not the caller's raw one) so the write always succeeds."""
    name = "½" * 222 + ".png"
    storage_path, _sha256, _size = save_bytes(cfg, 1, name, PNG_BYTES)
    resolved = resolve_path(cfg, storage_path)
    assert resolved.is_file()
    assert resolved.read_bytes() == PNG_BYTES
    basename = storage_path.rsplit("/", 1)[-1]
    assert len(basename.encode()) <= 255
    assert basename.endswith(".png")


def test_safe_join_rejects_traversal(cfg, tmp_path):
    root = cfg.artifact_root
    root.mkdir(parents=True, exist_ok=True)
    with pytest.raises(ValueError):
        safe_join(root, "../../etc/passwd")
    with pytest.raises(ValueError):
        safe_join(root, "1/../../../secrets.txt")
    # A sibling path that merely starts with the root's name but escapes must also be rejected.
    with pytest.raises(ValueError):
        safe_join(root, "../" + root.name + "-evil/x")


def test_resolve_path_rejects_traversal(cfg):
    with pytest.raises(ValueError):
        resolve_path(cfg, "../../../etc/passwd")


def test_guess_content_type_sniffs_magic_bytes_over_extension():
    # A PNG signature wins even if the filename lies about the extension.
    assert guess_content_type("evidence.txt", PNG_BYTES) == "image/png"
    assert guess_content_type("notes.txt", b"plain text body") == "text/plain"
    assert guess_content_type("mystery.bin", b"\x00\x01\x02") == "application/octet-stream"


# --------------------------------------------------------------------------- upload API


def test_upload_multipart_creates_row_and_file(client, session_factory, cfg):
    with session_factory() as db:
        eng = _make_engagement(db)
        finding = _make_finding(db, eng)
        engagement_id, finding_id = eng.id, finding.id

    resp = client.post(
        "/scribble/api/artifacts",
        data={
            "engagement_id": str(engagement_id),
            "finding_id": str(finding_id),
            "caption": "login screenshot",
            "file": (io.BytesIO(PNG_BYTES), "login.png"),
        },
        content_type="multipart/form-data",
    )
    assert resp.status_code == 201
    body = resp.get_json()
    assert body["kind"] == "screenshot"
    assert body["filename"] == "login.png"
    assert body["url"].endswith(f"/artifacts/{body['id']}/raw")

    with session_factory() as db:
        artifact = db.get(Artifact, uuid.UUID(body["id"]))
        assert artifact is not None
        assert artifact.engagement_id == engagement_id
        assert artifact.finding_id == finding_id
        assert artifact.caption == "login screenshot"
        assert artifact.content_type == "image/png"
        assert artifact.include_in_report is True
        path = resolve_path(cfg, artifact.storage_path)
        assert path.is_file()
        assert path.read_bytes() == PNG_BYTES


def test_create_artifact_drops_foreign_finding_id(client, session_factory):
    """ext#52: a `finding_id` belonging to a DIFFERENT engagement is silently dropped to None, not
    written through -- otherwise a caller holding engagement A could bolt evidence onto a finding in
    engagement B's report by naming B's finding id in the upload to A."""
    with session_factory() as db:
        eng_a = _make_engagement(db, client_name="Acme A")
        eng_b = _make_engagement(db, client_name="Acme B")
        foreign_finding = _make_finding(db, eng_b)
        engagement_id, foreign_finding_id = eng_a.id, foreign_finding.id

    resp = client.post(
        "/scribble/api/artifacts",
        data={
            "engagement_id": str(engagement_id),
            "finding_id": str(foreign_finding_id),
            "file": (io.BytesIO(PNG_BYTES), "shot.png"),
        },
        content_type="multipart/form-data",
    )
    assert resp.status_code == 201
    body = resp.get_json()
    assert body["finding_id"] is None
    assert body["finding_id_dropped"] is True

    with session_factory() as db:
        artifact = db.get(Artifact, body["id"])
        assert artifact.engagement_id == engagement_id
        assert artifact.finding_id is None


def test_create_artifact_keeps_own_finding_id(client, session_factory):
    """The companion positive: a `finding_id` in the SAME engagement is honored, not dropped."""
    with session_factory() as db:
        eng = _make_engagement(db)
        finding = _make_finding(db, eng)
        engagement_id, finding_id = eng.id, finding.id

    resp = client.post(
        "/scribble/api/artifacts",
        data={
            "engagement_id": str(engagement_id),
            "finding_id": str(finding_id),
            "file": (io.BytesIO(PNG_BYTES), "shot.png"),
        },
        content_type="multipart/form-data",
    )
    assert resp.status_code == 201
    body = resp.get_json()
    assert body["finding_id"] == str(finding_id)
    assert body["finding_id_dropped"] is False


def test_create_artifact_refuses_unparseable_finding_id(client, session_factory):
    """ext#52 point 2: a malformed `finding_id` (not a whole number in range) is a 400, not a silent
    `_as_int`-to-None drop -- a float or a bool must not be coerced into attaching to a finding the
    caller never named."""
    with session_factory() as db:
        eng = _make_engagement(db)
        engagement_id = eng.id

    resp = client.post(
        "/scribble/api/artifacts",
        data={
            "engagement_id": str(engagement_id),
            "finding_id": "2.9",
            "file": (io.BytesIO(PNG_BYTES), "shot.png"),
        },
        content_type="multipart/form-data",
    )
    assert resp.status_code == 400


def test_create_artifact_empty_finding_id_is_engagement_level(client, session_factory):
    """The multipart surface submits `finding_id=""` for an untouched field -- must mean
    "engagement-level evidence", not a parse error."""
    with session_factory() as db:
        eng = _make_engagement(db)
        engagement_id = eng.id

    resp = client.post(
        "/scribble/api/artifacts",
        data={
            "engagement_id": str(engagement_id),
            "finding_id": "",
            "file": (io.BytesIO(PNG_BYTES), "shot.png"),
        },
        content_type="multipart/form-data",
    )
    assert resp.status_code == 201
    body = resp.get_json()
    assert body["finding_id"] is None
    assert body["finding_id_dropped"] is False


def test_list_engagement_artifacts_returns_engagement_rows(client, session_factory):
    """ext#51: `GET /scribble/api/engagements/<id>/artifacts` lists every artifact on the engagement
    (finding-attached and engagement-level alike), not just one finding's gallery."""
    with session_factory() as db:
        eng = _make_engagement(db)
        finding = _make_finding(db, eng)
        engagement_id, finding_id = eng.id, finding.id

    attached = client.post(
        "/scribble/api/artifacts",
        data={
            "engagement_id": str(engagement_id),
            "finding_id": str(finding_id),
            "file": (io.BytesIO(PNG_BYTES), "attached.png"),
        },
        content_type="multipart/form-data",
    ).get_json()
    unattached = client.post(
        "/scribble/api/artifacts",
        data={
            "engagement_id": str(engagement_id),
            "file": (io.BytesIO(PNG_BYTES), "unattached.png"),
        },
        content_type="multipart/form-data",
    ).get_json()

    resp = client.get(f"/scribble/api/engagements/{engagement_id}/artifacts")
    assert resp.status_code == 200
    ids = {a["id"] for a in resp.get_json()["artifacts"]}
    assert ids == {attached["id"], unattached["id"]}


def test_upload_json_base64_creates_row(client, session_factory):
    import base64

    with session_factory() as db:
        eng = _make_engagement(db)
        engagement_id = eng.id

    resp = client.post(
        "/scribble/api/artifacts",
        json={
            "engagement_id": engagement_id,
            "filename": "notes.txt",
            "content_base64": base64.b64encode(b"recon notes").decode(),
        },
    )
    assert resp.status_code == 201
    body = resp.get_json()
    assert body["kind"] == "text"

    with session_factory() as db:
        artifact = db.get(Artifact, uuid.UUID(body["id"]))
        assert artifact.content_type == "text/plain"


def test_upload_created_by_none_without_host_hook(client, session_factory):
    with session_factory() as db:
        eng = _make_engagement(db)
        engagement_id = eng.id

    resp = client.post(
        "/scribble/api/artifacts",
        data={
            "engagement_id": str(engagement_id),
            "file": (io.BytesIO(PNG_BYTES), "login.png"),
        },
        content_type="multipart/form-data",
    )
    assert resp.status_code == 201
    with session_factory() as db:
        artifact = db.get(Artifact, uuid.UUID(resp.get_json()["id"]))
        assert artifact.created_by is None


def test_upload_created_by_set_from_host_current_actor_hook(client, session_factory, app):
    from types import SimpleNamespace

    cfg = app.extensions["scribble"]
    cfg.extras["current_actor"] = lambda: SimpleNamespace(username="j.analyst")
    try:
        with session_factory() as db:
            eng = _make_engagement(db)
            engagement_id = eng.id

        resp = client.post(
            "/scribble/api/artifacts",
            data={
                "engagement_id": str(engagement_id),
                "file": (io.BytesIO(PNG_BYTES), "login.png"),
            },
            content_type="multipart/form-data",
        )
        assert resp.status_code == 201
        with session_factory() as db:
            artifact = db.get(Artifact, uuid.UUID(resp.get_json()["id"]))
            assert artifact.created_by == "j.analyst"
    finally:
        cfg.extras.pop("current_actor", None)


def test_upload_with_placement_inline(client, session_factory):
    with session_factory() as db:
        eng = _make_engagement(db)
        engagement_id = eng.id

    resp = client.post(
        "/scribble/api/artifacts",
        data={
            "engagement_id": str(engagement_id),
            "placement": "inline",
            "file": (io.BytesIO(PNG_BYTES), "shot.png"),
        },
        content_type="multipart/form-data",
    )
    assert resp.status_code == 201
    with session_factory() as db:
        artifact = db.get(Artifact, uuid.UUID(resp.get_json()["id"]))
        assert artifact.placement == ArtifactPlacement.inline


def test_upload_with_invalid_placement_400(client, session_factory):
    with session_factory() as db:
        eng = _make_engagement(db)
        engagement_id = eng.id

    resp = client.post(
        "/scribble/api/artifacts",
        data={
            "engagement_id": str(engagement_id),
            "placement": "bogus",
            "file": (io.BytesIO(PNG_BYTES), "shot.png"),
        },
        content_type="multipart/form-data",
    )
    assert resp.status_code == 400


def test_upload_default_placement_is_attached(client, session_factory):
    with session_factory() as db:
        eng = _make_engagement(db)
        engagement_id = eng.id

    resp = client.post(
        "/scribble/api/artifacts",
        data={
            "engagement_id": str(engagement_id),
            "file": (io.BytesIO(PNG_BYTES), "shot.png"),
        },
        content_type="multipart/form-data",
    )
    assert resp.status_code == 201
    with session_factory() as db:
        artifact = db.get(Artifact, uuid.UUID(resp.get_json()["id"]))
        assert artifact.placement == ArtifactPlacement.attached


# --------------------------------------------------------------------------- idempotency


def test_upload_with_same_idempotency_key_header_returns_existing_artifact(client, session_factory):
    with session_factory() as db:
        eng = _make_engagement(db)
        engagement_id = eng.id

    first = client.post(
        "/scribble/api/artifacts",
        data={
            "engagement_id": str(engagement_id),
            "file": (io.BytesIO(PNG_BYTES), "shot.png"),
        },
        content_type="multipart/form-data",
        headers={"Idempotency-Key": "retry-key-1"},
    )
    assert first.status_code == 201
    first_id = uuid.UUID(first.get_json()["id"])

    # Retried upload with the SAME header: must not create a second row/file, and must return 200.
    second = client.post(
        "/scribble/api/artifacts",
        data={
            "engagement_id": str(engagement_id),
            "file": (io.BytesIO(PNG_BYTES), "shot.png"),
        },
        content_type="multipart/form-data",
        headers={"Idempotency-Key": "retry-key-1"},
    )
    assert second.status_code == 200
    assert uuid.UUID(second.get_json()["id"]) == first_id

    with session_factory() as db:
        count = db.query(Artifact).filter_by(engagement_id=engagement_id).count()
        assert count == 1


def test_upload_without_idempotency_key_creates_normally(client, session_factory):
    with session_factory() as db:
        eng = _make_engagement(db)
        engagement_id = eng.id

    first = client.post(
        "/scribble/api/artifacts",
        data={"engagement_id": str(engagement_id), "file": (io.BytesIO(PNG_BYTES), "a.png")},
        content_type="multipart/form-data",
    )
    second = client.post(
        "/scribble/api/artifacts",
        data={"engagement_id": str(engagement_id), "file": (io.BytesIO(PNG_BYTES), "b.png")},
        content_type="multipart/form-data",
    )
    assert first.status_code == 201
    assert second.status_code == 201
    assert first.get_json()["id"] != uuid.UUID(second.get_json()["id"])

    with session_factory() as db:
        count = db.query(Artifact).filter_by(engagement_id=engagement_id).count()
        assert count == 2


def test_upload_with_different_idempotency_keys_creates_two_artifacts(client, session_factory):
    with session_factory() as db:
        eng = _make_engagement(db)
        engagement_id = eng.id

    first = client.post(
        "/scribble/api/artifacts",
        data={"engagement_id": str(engagement_id), "file": (io.BytesIO(PNG_BYTES), "a.png")},
        content_type="multipart/form-data",
        headers={"Idempotency-Key": "key-a"},
    )
    second = client.post(
        "/scribble/api/artifacts",
        data={"engagement_id": str(engagement_id), "file": (io.BytesIO(PNG_BYTES), "b.png")},
        content_type="multipart/form-data",
        headers={"Idempotency-Key": "key-b"},
    )
    assert first.status_code == 201
    assert second.status_code == 201
    assert first.get_json()["id"] != uuid.UUID(second.get_json()["id"])

    with session_factory() as db:
        count = db.query(Artifact).filter_by(engagement_id=engagement_id).count()
        assert count == 2


def test_upload_requires_engagement_id(client):
    resp = client.post(
        "/scribble/api/artifacts",
        data={"file": (io.BytesIO(b"x"), "f.txt")},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 400


def test_upload_multipart_rejects_overlong_filename(client, session_factory):
    """#55 residual 2: the cookie upload path never bounded ``filename`` at all before this fix --
    unlike the machine route (``api_pat.py``), which already rejects an over-``SAFE_NAME_MAX`` name
    with a 400. Without a cap this route stores the caller's RAW filename straight into
    ``Artifact.filename`` (``String(512)``); a long enough one truncates silently on SQLite (this
    suite's backend) and raises ``StringDataRightTruncation`` on the Postgres prod actually runs.
    A too-long name must be refused before any byte is written, exactly like the machine route."""
    with session_factory() as db:
        eng = _make_engagement(db)
        engagement_id = eng.id

    long_name = "a" * (SAFE_NAME_MAX + 1) + ".png"
    resp = client.post(
        "/scribble/api/artifacts",
        data={
            "engagement_id": str(engagement_id),
            "file": (io.BytesIO(PNG_BYTES), long_name),
        },
        content_type="multipart/form-data",
    )
    assert resp.status_code == 400
    assert "too long" in resp.get_json()["error"]

    with session_factory() as db:
        assert db.query(Artifact).filter(Artifact.engagement_id == engagement_id).count() == 0


def test_upload_multipart_accepts_filename_at_the_cap(client, session_factory):
    """The cap must not become a false refusal for a name AT the boundary."""
    with session_factory() as db:
        eng = _make_engagement(db)
        engagement_id = eng.id

    name_at_cap = "b" * (SAFE_NAME_MAX - len(".png")) + ".png"
    assert len(name_at_cap) == SAFE_NAME_MAX
    resp = client.post(
        "/scribble/api/artifacts",
        data={
            "engagement_id": str(engagement_id),
            "file": (io.BytesIO(PNG_BYTES), name_at_cap),
        },
        content_type="multipart/form-data",
    )
    assert resp.status_code == 201, resp.get_json()


def test_upload_json_base64_rejects_overlong_filename(client, session_factory):
    """Same cap on the JSON-body upload shape, not just multipart."""
    import base64

    with session_factory() as db:
        eng = _make_engagement(db)
        engagement_id = eng.id

    long_name = "c" * (SAFE_NAME_MAX + 1) + ".png"
    resp = client.post(
        "/scribble/api/artifacts",
        json={
            "engagement_id": engagement_id,
            "filename": long_name,
            "content_base64": base64.b64encode(PNG_BYTES).decode(),
        },
    )
    assert resp.status_code == 400
    assert "too long" in resp.get_json()["error"]


def test_raw_download_is_forced_attachment(client, session_factory):
    with session_factory() as db:
        eng = _make_engagement(db)
        engagement_id = eng.id

    create = client.post(
        "/scribble/api/artifacts",
        data={"engagement_id": str(engagement_id), "file": (io.BytesIO(PNG_BYTES), "shot.png")},
        content_type="multipart/form-data",
    )
    artifact_id = uuid.UUID(create.get_json()["id"])

    resp = client.get(f"/scribble/api/artifacts/{artifact_id}/raw")
    assert resp.status_code == 200
    assert resp.data == PNG_BYTES
    disposition = resp.headers.get("Content-Disposition", "")
    assert "attachment" in disposition


def test_raw_download_missing_returns_404(client):
    resp = client.get("/scribble/api/artifacts/{_MISSING_ID}/raw")
    assert resp.status_code == 404


# --------------------------------------------------------------------------- include/exclude + caption


def test_update_caption_and_include_toggle(client, session_factory):
    with session_factory() as db:
        eng = _make_engagement(db)
        engagement_id = eng.id

    create = client.post(
        "/scribble/api/artifacts",
        data={"engagement_id": str(engagement_id), "file": (io.BytesIO(PNG_BYTES), "shot.png")},
        content_type="multipart/form-data",
    )
    artifact_id = uuid.UUID(create.get_json()["id"])

    resp = client.post(
        f"/scribble/api/artifacts/{artifact_id}",
        json={"caption": "updated caption", "include_in_report": False},
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["caption"] == "updated caption"
    assert body["include_in_report"] is False

    with session_factory() as db:
        artifact = db.get(Artifact, artifact_id)
        assert artifact.caption == "updated caption"
        assert artifact.include_in_report is False


def test_update_missing_artifact_404(client):
    resp = client.post("/scribble/api/artifacts/{_MISSING_ID}", json={"caption": "x"})
    assert resp.status_code == 404


# --------------------------------------------------------------------------- reorder


def test_reorder_persists_order_index(client, session_factory):
    with session_factory() as db:
        eng = _make_engagement(db)
        finding = _make_finding(db, eng)
        engagement_id, finding_id = eng.id, finding.id

    ids = []
    for name in ("first.png", "second.png", "third.png"):
        resp = client.post(
            "/scribble/api/artifacts",
            data={
                "engagement_id": str(engagement_id),
                "finding_id": str(finding_id),
                "file": (io.BytesIO(PNG_BYTES), name),
            },
            content_type="multipart/form-data",
        )
        ids.append(uuid.UUID(resp.get_json()["id"]))

    # Reverse the natural creation order.
    new_order = list(reversed(ids))
    resp = client.post(f"/scribble/api/findings/{finding_id}/artifacts/reorder", json={"order": new_order})
    assert resp.status_code == 200

    listing = client.get(f"/scribble/api/findings/{finding_id}/artifacts").get_json()
    assert [uuid.UUID(a["id"]) for a in listing["artifacts"]] == new_order

    with session_factory() as db:
        rows = {a.id: a.order_index for a in db.query(Artifact).filter_by(finding_id=finding_id).all()}
        for position, artifact_id in enumerate(new_order):
            assert rows[artifact_id] == position


def test_reorder_requires_order_list(client, session_factory):
    with session_factory() as db:
        eng = _make_engagement(db)
        finding = _make_finding(db, eng)
        finding_id = finding.id

    resp = client.post(f"/scribble/api/findings/{finding_id}/artifacts/reorder", json={"order": "nope"})
    assert resp.status_code == 400


# --------------------------------------------------------------------------- delete


def test_delete_removes_row_and_file(client, session_factory, cfg):
    with session_factory() as db:
        eng = _make_engagement(db)
        engagement_id = eng.id

    create = client.post(
        "/scribble/api/artifacts",
        data={"engagement_id": str(engagement_id), "file": (io.BytesIO(PNG_BYTES), "shot.png")},
        content_type="multipart/form-data",
    )
    artifact_id = uuid.UUID(create.get_json()["id"])

    with session_factory() as db:
        storage_path = db.get(Artifact, artifact_id).storage_path
        on_disk = resolve_path(cfg, storage_path)
        assert on_disk.is_file()

    resp = client.post(f"/scribble/api/artifacts/{artifact_id}/delete")
    assert resp.status_code == 200
    assert resp.get_json()["ok"] is True

    with session_factory() as db:
        assert db.get(Artifact, artifact_id) is None
    assert not on_disk.exists()


def test_delete_missing_artifact_404(client):
    resp = client.post("/scribble/api/artifacts/{_MISSING_ID}/delete")
    assert resp.status_code == 404


# --------------------------------------------------------------------------- list


def test_list_finding_artifacts_ordered(client, session_factory):
    with session_factory() as db:
        eng = _make_engagement(db)
        finding = _make_finding(db, eng)
        engagement_id, finding_id = eng.id, finding.id

    for name in ("a.png", "b.png"):
        client.post(
            "/scribble/api/artifacts",
            data={
                "engagement_id": str(engagement_id),
                "finding_id": str(finding_id),
                "file": (io.BytesIO(PNG_BYTES), name),
            },
            content_type="multipart/form-data",
        )

    resp = client.get(f"/scribble/api/findings/{finding_id}/artifacts")
    assert resp.status_code == 200
    body = resp.get_json()
    assert len(body["artifacts"]) == 2
    assert [a["filename"] for a in body["artifacts"]] == ["a.png", "b.png"]


# --------------------------------------------------------------------------- report context integration


def test_excluded_artifact_filtered_from_report_context(session_factory, app):
    with session_factory() as db:
        eng = _make_engagement(db)
        group = FindingGroup(engagement=eng, name="External", order_index=0)
        finding = EngagementFinding(engagement=eng, group=group, title="xss", severity=Severity.high)
        db.add(group)
        db.add(finding)
        db.commit()

        included = Artifact(
            engagement=eng,
            finding=finding,
            filename="kept.png",
            storage_path="x/kept.png",
            include_in_report=True,
            order_index=0,
        )
        excluded = Artifact(
            engagement=eng,
            finding=finding,
            filename="dropped.png",
            storage_path="x/dropped.png",
            include_in_report=False,
            order_index=1,
        )
        db.add(included)
        db.add(excluded)
        db.commit()

        with app.app_context():
            ctx = build_report_context(eng, artifact_url=artifact_url)

    assert len(ctx.groups) == 1
    finding_ctx = ctx.groups[0].findings[0]
    filenames = [a.filename for a in finding_ctx.artifacts]
    assert filenames == ["kept.png"]
    assert "dropped.png" not in filenames


def test_foreign_engagement_artifact_excluded_from_finding_gallery(session_factory, app):
    """ext#52 point 4 (defence in depth): `_finding_ctx` must never render an artifact whose
    `engagement_id` differs from the finding's own engagement, even if one somehow slipped past the
    write-time tenancy check -- e.g. a direct DB insert, or a future regression in `create_artifact`'s
    cross-check. Every legitimate artifact already carries the finding's own `engagement_id` (both
    upload routes set it that way), so this filter can never drop a real one."""
    with session_factory() as db:
        eng_a = _make_engagement(db, client_name="Acme A")
        eng_b = _make_engagement(db, client_name="Acme B")
        group = FindingGroup(engagement=eng_a, name="External", order_index=0)
        finding = EngagementFinding(engagement=eng_a, group=group, title="xss", severity=Severity.high)
        db.add(group)
        db.add(finding)
        db.commit()

        legit = Artifact(
            engagement=eng_a,
            finding=finding,
            filename="legit.png",
            storage_path="x/legit.png",
            include_in_report=True,
            order_index=0,
        )
        # Force-attached: same trick a write-time bug or a direct DB write could produce -- an
        # Artifact row pointed at eng_b but linked to eng_a's finding.
        foreign = Artifact(
            engagement=eng_b,
            finding=finding,
            filename="foreign.png",
            storage_path="x/foreign.png",
            include_in_report=True,
            order_index=1,
        )
        db.add(legit)
        db.add(foreign)
        db.commit()

        with app.app_context():
            ctx = build_report_context(eng_a, artifact_url=artifact_url)

    assert len(ctx.groups) == 1
    finding_ctx = ctx.groups[0].findings[0]
    filenames = [a.filename for a in finding_ctx.artifacts]
    assert filenames == ["legit.png"]
    assert "foreign.png" not in filenames


def test_artifact_url_builds_raw_endpoint(app):
    with app.test_request_context():
        url = artifact_url(42)
    assert url.endswith("/artifacts/42/raw")
    assert url.startswith("/scribble/api")


def test_upload_with_same_engagement_finding_id_echoes_it_undropped(client, session_factory):
    """#40 mechanism 3: a valid, SAME-engagement ``finding_id`` is echoed back verbatim with
    ``finding_id_dropped: false``."""
    with session_factory() as db:
        eng = _make_engagement(db)
        finding = _make_finding(db, eng)
        engagement_id, finding_id = eng.id, finding.id

    resp = client.post(
        "/scribble/api/artifacts",
        json={
            "engagement_id": engagement_id,
            "finding_id": finding_id,
            "filename": "x.png",
            "content_base64": "eA==",
        },
    )
    assert resp.status_code == 201
    body = resp.get_json()
    assert body["finding_id"] == str(finding_id)
    assert body["finding_id_dropped"] is False

    with session_factory() as db:
        artifact = db.get(Artifact, body["id"])
        assert artifact.finding_id == finding_id


def test_upload_with_cross_engagement_finding_id_is_nulled_and_flagged(client, session_factory):
    """#40 mechanism 3: ``finding_id`` naming a finding on a DIFFERENT engagement must not be stored
    verbatim -- before this fix, an authenticated actor holding ANY engagement could bolt evidence onto
    a finding in someone else's report by just naming its id, and that finding's report would silently
    carry the attacker-supplied artifact. The upload itself still succeeds (matches the ``group_id``
    precedent elsewhere in this package): the association is dropped, not the whole request."""
    with session_factory() as db:
        eng_a = _make_engagement(db)
        eng_b = Engagement(name="Other Co Engagement")
        db.add(eng_b)
        db.commit()
        foreign_finding = _make_finding(db, eng_b)
        engagement_id, foreign_finding_id = eng_a.id, foreign_finding.id

    resp = client.post(
        "/scribble/api/artifacts",
        json={
            "engagement_id": engagement_id,
            "finding_id": foreign_finding_id,
            "filename": "x.png",
            "content_base64": "eA==",
        },
    )
    assert resp.status_code == 201
    body = resp.get_json()
    assert body["finding_id"] is None
    assert body["finding_id_dropped"] is True

    with session_factory() as db:
        artifact = db.get(Artifact, body["id"])
        assert artifact.engagement_id == engagement_id
        assert artifact.finding_id is None
