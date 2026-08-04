"""Report route authorization (`scribble/report_html_api.py::_authorize_engagement_view`).

Ported from the deleted lotek `tests/test_scribble_report_authz.py` (adversarial review 2026-07-27,
CRIT-4: the live report + its export embed a client's findings/evidence, so a bare `db.get(Engagement,
id)` with no ownership check would let ANY authenticated user read ANY engagement's report by walking
the id). The check itself lives entirely in scribble (`cfg.extras['host']` truthy -> enforce; admins
see everything; a non-admin sees only engagements it OWNS; a NULL owner is admin-only), so this is
exercised against the `stub_host` fixture's `current_actor` hook, not a real lotek login.
"""

from __future__ import annotations

import scribble.models as fm
from tests.conftest import StubUser, _StubRole

UI = "/scribble"


def _make_engagement(session_factory, *, owner_id) -> int:
    with session_factory() as db:
        eng = fm.Engagement(name="owned engagement", scope_type="external", owner_id=owner_id)
        db.add(eng)
        db.commit()
        return eng.id


def test_non_owner_viewer_cannot_read_report(client, stub_host, session_factory):
    eid = _make_engagement(session_factory, owner_id=7)
    stub_host.current_user = StubUser(id=8, username="some-viewer", role=_StubRole("viewer"))
    assert client.get(f"{UI}/engagements/{eid}/report").status_code == 404


def test_non_owner_operator_cannot_read_report(client, stub_host, session_factory):
    eid = _make_engagement(session_factory, owner_id=7)
    stub_host.current_user = StubUser(id=9, username="other-op", role=_StubRole("operator"))
    assert client.get(f"{UI}/engagements/{eid}/report").status_code == 404


def test_owner_operator_can_read_own_report(client, stub_host, session_factory):
    eid = _make_engagement(session_factory, owner_id=7)
    stub_host.current_user = StubUser(id=7, username="owner-op", role=_StubRole("operator"))
    assert client.get(f"{UI}/engagements/{eid}/report").status_code == 200


def test_admin_can_read_any_report(client, stub_host, session_factory):
    eid = _make_engagement(session_factory, owner_id=7)
    stub_host.current_user = StubUser(id=1, username="admin", role=_StubRole("admin"))
    assert client.get(f"{UI}/engagements/{eid}/report").status_code == 200


def test_export_also_scoped(client, stub_host, session_factory):
    eid = _make_engagement(session_factory, owner_id=7)
    stub_host.current_user = StubUser(id=8, username="some-viewer2", role=_StubRole("viewer"))
    assert client.get(f"{UI}/engagements/{eid}/report/export").status_code == 404


def test_null_owner_is_admin_only(client, stub_host, session_factory):
    """A legacy/unknown-owner engagement (owner_id NULL) is the SECURE default: admin-only, not
    visible to any non-admin (there is no owner to match against)."""
    eid = _make_engagement(session_factory, owner_id=None)
    stub_host.current_user = StubUser(id=7, username="opA", role=_StubRole("operator"))
    assert client.get(f"{UI}/engagements/{eid}/report").status_code == 404

    stub_host.current_user = StubUser(id=1, username="admin", role=_StubRole("admin"))
    assert client.get(f"{UI}/engagements/{eid}/report").status_code == 200


def test_standalone_no_host_applies_no_authorization(client, session_factory):
    """Without `stub_host` wired (`cfg.extras['host']` absent), standalone Scribble has no host
    authorization model to apply -- the report is reachable regardless of ownership."""
    eid = _make_engagement(session_factory, owner_id=7)
    assert client.get(f"{UI}/engagements/{eid}/report").status_code == 200
