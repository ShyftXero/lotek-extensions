"""Report route authorization for the ``.docx`` download
(`scribble/report_docx_api.py::engagement_report_docx`).

Regression guard for a cross-tenant IDOR: unlike its sibling `/report` HTML route
(`report_html_api.py`), the `.docx` route built and streamed the report with **no tenancy check at
all** — it never called `_authorize_engagement_view`. Any authenticated actor who could guess or
enumerate an `engagement_id` could download another client's findings and evidence as a `.docx`.
Found (and traced back to this repo's `main`) while re-vendoring Scribble onto lotek v2, during that
branch's mandatory adversarial review.

Mirrors `tests/test_scribble_report_authz.py`'s fixture shape and cases (client-scoped grants via
`stub_host.viewable_client_ids`, admin bypass, NULL-client default, standalone-no-host, and the
host-missing-the-capability fail-closed case) applied to the docx route instead of the HTML one. Kept
deliberately asymmetric with that file: this one exists to prove the docx route calls the SAME guard,
not to re-litigate `_authorize_engagement_view`'s own behavior (already covered there).
"""

from __future__ import annotations

import io

import docx

import scribble.models as fm
from tests.conftest import StubUser, _StubRole

UI = "/scribble"

ACME = 42          # the client under test
OTHER_CLIENT = 99  # a client the actor holds no grant under


def _make_engagement(session_factory, *, client_id, owner_id=None) -> int:
    with session_factory() as db:
        eng = fm.Engagement(
            name="engagement under test", scope_type="external",
            owner_id=owner_id, client_id=client_id,
        )
        db.add(eng)
        db.commit()
        return eng.id


# ── DENY ───────────────────────────────────────────────────────────────────────


def test_viewer_without_a_client_grant_cannot_download_docx(client, stub_host, session_factory):
    eid = _make_engagement(session_factory, client_id=ACME)
    stub_host.current_user = StubUser(id=8, username="some-viewer", role=_StubRole("viewer"))
    stub_host.viewable_client_ids = set()
    assert client.get(f"{UI}/engagements/{eid}/report.docx").status_code == 404


def test_a_grant_on_another_client_does_not_carry_to_docx(client, stub_host, session_factory):
    """The gate is per-client, so holding one client must not open another."""
    eid = _make_engagement(session_factory, client_id=ACME)
    stub_host.current_user = StubUser(id=9, username="op-elsewhere", role=_StubRole("operator"))
    stub_host.viewable_client_ids = {OTHER_CLIENT}
    assert client.get(f"{UI}/engagements/{eid}/report.docx").status_code == 404


def test_engagement_with_no_client_is_admin_only_for_docx(client, stub_host, session_factory):
    """`client_id IS NULL` carries nothing to attribute a read to, so it is admin-only — even for an
    operator holding every client grant there is, and even when it is the engagement's own owner."""
    eid = _make_engagement(session_factory, client_id=None, owner_id=7)

    stub_host.current_user = StubUser(id=7, username="op-owner", role=_StubRole("operator"))
    stub_host.viewable_client_ids = {ACME, OTHER_CLIENT}
    assert client.get(f"{UI}/engagements/{eid}/report.docx").status_code == 404

    stub_host.current_user = StubUser(id=1, username="admin2", role=_StubRole("admin"))
    assert client.get(f"{UI}/engagements/{eid}/report.docx").status_code == 200


def test_a_host_missing_the_capability_fails_closed_for_docx(client, stub_host, session_factory):
    """The regression guard for the defect this module's sibling already proved once: a host bundle
    that predates `can_view_client` must be REFUSED, not fall back to a local rule."""
    eid = _make_engagement(session_factory, client_id=ACME)
    stub_host.current_user = StubUser(id=1, username="admin3", role=_StubRole("admin"))
    del client.application.extensions["scribble"].extras["can_view_client"]
    assert client.get(f"{UI}/engagements/{eid}/report.docx").status_code == 404


# ── ALLOW — the cases that distinguish a working gate from a dead/ungated route ─────────────────────


def test_operator_with_a_client_grant_can_download_docx(client, stub_host, session_factory):
    eid = _make_engagement(session_factory, client_id=ACME)
    stub_host.current_user = StubUser(id=7, username="op-granted", role=_StubRole("operator"))
    stub_host.viewable_client_ids = {ACME}
    resp = client.get(f"{UI}/engagements/{eid}/report.docx")
    assert resp.status_code == 200
    # It's a real, openable docx — not just a 200 with an empty/garbage body.
    docx.Document(io.BytesIO(resp.data))


def test_admin_can_download_docx_for_any_client(client, stub_host, session_factory):
    eid = _make_engagement(session_factory, client_id=ACME)
    stub_host.current_user = StubUser(id=1, username="admin", role=_StubRole("admin"))
    stub_host.viewable_client_ids = set()  # no explicit grant — admin does not need one
    assert client.get(f"{UI}/engagements/{eid}/report.docx").status_code == 200


# ── the host-absent case ────────────────────────────────────────────────────────


def test_standalone_no_host_applies_no_authorization_to_docx(client, session_factory):
    """Without `stub_host` wired (`cfg.extras['host']` absent), standalone Scribble has no host
    authorization model to apply — the docx report is reachable regardless of client."""
    eid = _make_engagement(session_factory, client_id=ACME)
    assert client.get(f"{UI}/engagements/{eid}/report.docx").status_code == 200
