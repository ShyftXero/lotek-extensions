"""The kit editor's inline-image artifact endpoints: POST ``/<rid>/api/artifacts`` + GET ``…/raw``.

They are thin wrappers over the SAME attachment store the manual upload uses, so the access + content
defences proven in ``test_attachments.py`` apply unchanged. Here we pin the editor-facing CONTRACT
(JSON ``{id, url}``, the raw route serves the stored bytes, the ``report_id`` in the raw path must own
the attachment) and confirm the magic-byte defence still holds on this path.
"""

from __future__ import annotations

import io
import uuid

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
HTML = b"<html><script>alert(1)</script></html>"


def _report(client, session_factory):
    client.post("/bugreport/", data={"title": "t", "body": "b"})
    from bugreport.models import Report

    with session_factory() as db:
        return db.query(Report).one().id


def _post_artifact(client, rid, data, name, ctype):
    return client.post(
        f"/bugreport/{rid}/api/artifacts",
        data={"file": (io.BytesIO(data), name, ctype)},
        content_type="multipart/form-data",
    )


def test_upload_artifact_returns_id_url_and_raw_serves_the_bytes(client, session_factory, blobs):
    rid = _report(client, session_factory)
    resp = _post_artifact(client, rid, PNG, "shot.png", "image/png")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["id"]
    assert body["url"].endswith(f"/api/artifacts/{body['id']}/raw")
    raw = client.get(body["url"])
    assert raw.status_code == 200
    assert raw.headers["Content-Type"].startswith("image/png")
    assert raw.get_data() == PNG


def test_raw_url_report_id_must_own_the_attachment(client, session_factory, blobs):
    rid = _report(client, session_factory)
    aid = _post_artifact(client, rid, PNG, "s.png", "image/png").get_json()["id"]
    # a different report id in the path -> 404 (no existence oracle), even though the attachment exists
    assert client.get(f"/bugreport/{uuid.uuid4()}/api/artifacts/{aid}/raw").status_code == 404
    # the owning report id -> 200
    assert client.get(f"/bugreport/{rid}/api/artifacts/{aid}/raw").status_code == 200


def test_missing_file_is_a_400(client, session_factory, blobs):
    rid = _report(client, session_factory)
    resp = client.post(
        f"/bugreport/{rid}/api/artifacts", data={}, content_type="multipart/form-data"
    )
    assert resp.status_code == 400


def test_html_as_png_through_the_artifact_route_is_not_served_as_an_image(client, session_factory, blobs):
    # evil.html labelled image/png lands as an opaque octet-stream (magic bytes decide), never an inline
    # image — the same defence test_attachments pins for the manual upload, proven on the editor path.
    rid = _report(client, session_factory)
    aid = _post_artifact(client, rid, HTML, "evil.png", "image/png").get_json()["id"]
    raw = client.get(f"/bugreport/{rid}/api/artifacts/{aid}/raw")
    assert raw.headers["Content-Type"].startswith("application/octet-stream")
