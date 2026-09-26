"""Self-gated report-library routes: the cover-logo upload, the section-preset CRUD and the AI rephrase
carry NO engagement id in their URL, so ``scribble.authz._gate`` skips them entirely — both its write-
capability check AND (for the upload's form-body engagement) its view check. Each route re-applies the
missing check by hand; these tests pin that.

Standalone Scribble is fail-open (no host authorization model), so a plain ``client`` cannot prove a
refusal — every one of these tests mounts a ``stub_host`` and drives it, exactly as
``test_scribble_write_operate_authz.py`` does for the machine blueprint.

Red → green: drop the ``if not host_can_write(): abort(403)`` guards (and the ``can_view_engagement``
check in ``upload_report_logo``) and every refusal below flips to 302/200/201 and the writes land — the
viewer-writes-shared-library hole, and the cross-tenant cover-logo IDOR (a global writer setting any
tenant's cover), that the self-gates close.
"""
from __future__ import annotations

import io
import uuid
from types import SimpleNamespace

import scribble.models as fm


def _identity(username: str, role_name: str):
    """A browser-session identity duck-typed to what the cookie path reads: ``.id``/``.username`` and a
    ``.role`` whose ``.is_admin()`` decides the ``StubHost.can_view_client`` admin bypass. Built inline
    rather than importing ``tests.conftest.StubUser`` so this file is self-contained (and dodges pyrefly's
    single-file ``tests.conftest`` resolution gap the sibling authz tests carry)."""
    role = SimpleNamespace(name=role_name, is_admin=lambda: role_name == "admin",
                           can_write=lambda: role_name != "viewer")
    return SimpleNamespace(id=7 if role_name != "viewer" else 8, username=username, role=role)

UI = "/scribble"
API = "/scribble/api"
CLIENT = uuid.uuid7()

_PNG = bytes.fromhex(  # a real 1x1 PNG
    "89504e470d0a1a0a0000000d49484452000000010000000108020000009077"
    "53de0000000c4944415478da6360606000000005000166ff0f0e0000000049454e44ae426082"
)


def _engagement(session_factory, *, client_id=CLIENT):
    with session_factory() as db:
        eng = fm.ReportBoard(name="E", client_id=client_id)
        db.add(eng)
        db.commit()
        return eng.id


def _writer(stub_host, *, viewable_client_ids=frozenset()):
    """A write-capable, NON-admin operator, viewing only the named clients (empty = views nothing).
    Non-admin matters: ``StubHost.can_view_client`` grants an admin every client, which would mask the
    tenancy check. Cookie routes read ``current_user`` (the browser identity), not ``actor`` (the PAT)."""
    stub_host.current_user = _identity("op", "operator")
    stub_host.can_write_value = True
    stub_host.viewable_client_ids = set(viewable_client_ids)


def _viewer(stub_host, *, viewable_client_ids=frozenset({CLIENT})):
    """A read-only caller: may view its client, holds no write capability."""
    stub_host.current_user = _identity("viewer", "viewer")
    stub_host.can_write_value = False
    stub_host.viewable_client_ids = set(viewable_client_ids)


# ── CRITICAL-1: upload_report_logo cross-tenant cover write (the IDOR) ──────────────────────────────────

def test_upload_logo_cross_tenant_engagement_is_refused(client, stub_host, session_factory):
    """A GLOBAL writer (passes the write-cap gate) that may NOT view this engagement's client must not be
    able to set its cover logo by naming it in the form body — the exact hole the URL gate misses."""
    eng_id = _engagement(session_factory)
    _writer(stub_host, viewable_client_ids=frozenset())  # write cap, but views nothing

    resp = client.post(f"{UI}/report/logos", data={
        "engagement_id": str(eng_id), "label": "evil", "file": (io.BytesIO(_PNG), "x.png"),
    }, content_type="multipart/form-data")

    assert resp.status_code == 404, resp.status_code  # fail-closed like the gate — not a 302
    with session_factory() as db:
        assert db.get(fm.ReportBoard, eng_id).cover_logo_id is None      # cover untouched
        assert db.query(fm.ScribbleReportLogo).count() == 0             # and the insert rolled back


def test_upload_logo_same_tenant_engagement_is_allowed(client, stub_host, session_factory):
    """Positive control that keeps the refusal above honest: a writer who CAN view the client uploads and
    the cover is set."""
    eng_id = _engagement(session_factory)
    _writer(stub_host, viewable_client_ids=frozenset({CLIENT}))

    resp = client.post(f"{UI}/report/logos", data={
        "engagement_id": str(eng_id), "label": "brand", "file": (io.BytesIO(_PNG), "b.png"),
    }, content_type="multipart/form-data")

    assert resp.status_code == 302, resp.status_code
    with session_factory() as db:
        logos = db.query(fm.ScribbleReportLogo).all()
        assert len(logos) == 1
        assert db.get(fm.ReportBoard, eng_id).cover_logo_id == logos[0].id


def test_upload_logo_refused_without_write_capability(client, stub_host, session_factory):
    """A viewer cannot add to the org-wide logo library at all (write-cap gate, before any engagement)."""
    _viewer(stub_host)
    resp = client.post(f"{UI}/report/logos", data={"file": (io.BytesIO(_PNG), "x.png")},
                       content_type="multipart/form-data")
    assert resp.status_code == 403, resp.status_code
    with session_factory() as db:
        assert db.query(fm.ScribbleReportLogo).count() == 0


# ── WARNING-1: viewer must not mutate the shared section-preset library ─────────────────────────────────

def test_save_section_preset_refused_without_write_capability(client, stub_host, session_factory):
    _viewer(stub_host)
    resp = client.post(f"{API}/report/section-presets",
                       json={"name": "mine", "order": [{"key": "findings", "enabled": True}]})
    assert resp.status_code == 403, resp.status_code
    with session_factory() as db:
        assert db.query(fm.ScribbleSectionPreset).count() == 0


def test_delete_section_preset_refused_without_write_capability(client, stub_host, session_factory):
    with session_factory() as db:
        preset = fm.ScribbleSectionPreset(name="keep", specs=[{"key": "findings", "enabled": True}],
                                          fingerprint="fp-keep")
        db.add(preset)
        db.commit()
        pid = preset.id
    _viewer(stub_host)
    resp = client.post(f"{UI}/report/section-presets/{pid}/delete")
    assert resp.status_code == 403, resp.status_code
    with session_factory() as db:
        assert db.get(fm.ScribbleSectionPreset, pid) is not None  # survived


# ── WARNING-1/2: viewer must not drive the metered AI rephrase proxy ────────────────────────────────────

def test_rephrase_refused_without_write_capability(client, stub_host):
    """The write-cap gate is checked BEFORE the AI hook is even resolved, so a viewer is refused whether or
    not AI is enabled on the install."""
    _viewer(stub_host)
    resp = client.post(f"{API}/report/prose/rephrase", json={"text": "some prose", "field": "methodology"})
    assert resp.status_code == 403, resp.status_code
