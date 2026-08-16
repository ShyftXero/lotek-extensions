"""Report route authorization (`scribble/report_html_api.py::_authorize_engagement_view`).

Originally ported from the deleted lotek `tests/test_scribble_report_authz.py` (adversarial review
2026-07-27, CRIT-4: the live report + its export embed a client's findings/evidence, so a bare
`db.get(Engagement, id)` with no ownership check would let ANY authenticated user read ANY engagement's
report by walking the id).

**The axis changed, and these tests changed with it (2026-08-05).** The check used to be a hand-written
copy of lotek's `user_can_view_job` living in this module — *"admins see everything; a non-admin sees only
engagements it OWNS; a NULL owner is admin-only"*. That copy was removed in favour of asking the host,
because a copied predicate does not merely drift: this one had **inverted** relative to per-engagement
membership. The module now calls `cfg.extras["can_view_client"](engagement.client_id, actor)` and
`abort(404)`s when the host does not provide the key.

That refactor shipped with **only its extension half**: no host injected `can_view_client`, so every
mounted report 404'd for every actor, admin included — and this file recorded it as three failing ALLOW
cases while its DENY cases passed, because a dead route denies everyone. The host half landed in lotek as
`app/access.py::user_can_view_client` + `HostServices.can_view_client`.

So the assertions below are now **client**-scoped, not engagement-owner-scoped, and the two consequences
are asserted rather than left implicit:

* An engagement's readability follows its **client**, so a grant covers every engagement under it.
* An engagement with **no client** (`client_id IS NULL`) is **admin-only** — it carries nothing to
  attribute a read to. That is the secure default the host uses everywhere else.

`Engagement.owner_id` is ATTRIBUTION, never an authorization key (see the model) — no test here should
reintroduce it as one.
"""

from __future__ import annotations

import uuid

import scribble.models as fm
from tests.conftest import StubUser, _StubRole

UI = "/scribble"

# Scribble's own client PK is UUIDv7 since lotek#335. Where a test seeds `scribble_clients` and
# ALSO grants on the same id via the stub host, both halves must move together.
ACME = uuid.uuid7()          # the client under test
OTHER_CLIENT = uuid.uuid7()  # a client the actor holds no grant under


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
# These passed even while the route was dead, so on their own they prove nothing. Kept because they
# still have to hold now that it is alive.


def test_viewer_without_a_client_grant_cannot_read_report(client, stub_host, session_factory):
    eid = _make_engagement(session_factory, client_id=ACME)
    stub_host.current_user = StubUser(id=8, username="some-viewer", role=_StubRole("viewer"))
    stub_host.viewable_client_ids = set()
    assert client.get(f"{UI}/engagements/{eid}/report").status_code == 404


def test_operator_without_a_client_grant_cannot_read_report(client, stub_host, session_factory):
    eid = _make_engagement(session_factory, client_id=ACME)
    stub_host.current_user = StubUser(id=9, username="other-op", role=_StubRole("operator"))
    stub_host.viewable_client_ids = set()
    assert client.get(f"{UI}/engagements/{eid}/report").status_code == 404


def test_a_grant_on_another_client_does_not_carry(client, stub_host, session_factory):
    """The gate is per-client, so holding one client must not open another."""
    eid = _make_engagement(session_factory, client_id=ACME)
    stub_host.current_user = StubUser(id=9, username="op-elsewhere", role=_StubRole("operator"))
    stub_host.viewable_client_ids = {OTHER_CLIENT}
    assert client.get(f"{UI}/engagements/{eid}/report").status_code == 404


def test_export_is_scoped_too(client, stub_host, session_factory):
    """Both guarded routes, not just the viewable one — the export carries the same bytes."""
    eid = _make_engagement(session_factory, client_id=ACME)
    stub_host.current_user = StubUser(id=8, username="some-viewer2", role=_StubRole("viewer"))
    stub_host.viewable_client_ids = set()
    assert client.get(f"{UI}/engagements/{eid}/report/export").status_code == 404


# ── ALLOW — the cases that distinguish a working gate from a dead route ─────────────────────────────


def test_operator_with_a_client_grant_can_read_report(client, stub_host, session_factory):
    eid = _make_engagement(session_factory, client_id=ACME)
    stub_host.current_user = StubUser(id=7, username="op-granted", role=_StubRole("operator"))
    stub_host.viewable_client_ids = {ACME}
    assert client.get(f"{UI}/engagements/{eid}/report").status_code == 200


def test_export_is_reachable_with_a_grant(client, stub_host, session_factory):
    eid = _make_engagement(session_factory, client_id=ACME)
    stub_host.current_user = StubUser(id=7, username="op-granted2", role=_StubRole("operator"))
    stub_host.viewable_client_ids = {ACME}
    assert client.get(f"{UI}/engagements/{eid}/report/export").status_code == 200


def test_admin_can_read_any_client(client, stub_host, session_factory):
    eid = _make_engagement(session_factory, client_id=ACME)
    stub_host.current_user = StubUser(id=1, username="admin", role=_StubRole("admin"))
    stub_host.viewable_client_ids = set()  # no explicit grant — admin does not need one
    assert client.get(f"{UI}/engagements/{eid}/report").status_code == 200


def test_a_grant_covers_every_engagement_under_that_client(client, stub_host, session_factory):
    """Client granularity, stated as a test so the looseness is a recorded decision, not a surprise."""
    first = _make_engagement(session_factory, client_id=ACME)
    second = _make_engagement(session_factory, client_id=ACME)
    stub_host.current_user = StubUser(id=7, username="op-both", role=_StubRole("operator"))
    stub_host.viewable_client_ids = {ACME}
    assert client.get(f"{UI}/engagements/{first}/report").status_code == 200
    assert client.get(f"{UI}/engagements/{second}/report").status_code == 200


# ── the NULL-client default ────────────────────────────────────────────────────


def test_engagement_with_no_client_is_admin_only(client, stub_host, session_factory):
    """`client_id IS NULL` carries nothing to attribute a read to, so it is admin-only — even for an
    operator holding every client grant there is, and even when it is the engagement's own owner."""
    eid = _make_engagement(session_factory, client_id=None, owner_id=7)

    stub_host.current_user = StubUser(id=7, username="op-owner", role=_StubRole("operator"))
    stub_host.viewable_client_ids = {ACME, OTHER_CLIENT}
    assert client.get(f"{UI}/engagements/{eid}/report").status_code == 404

    stub_host.current_user = StubUser(id=1, username="admin2", role=_StubRole("admin"))
    assert client.get(f"{UI}/engagements/{eid}/report").status_code == 200


# ── the host-absent cases ──────────────────────────────────────────────────────


def test_standalone_no_host_applies_no_authorization(client, session_factory):
    """Without `stub_host` wired (`cfg.extras['host']` absent), standalone Scribble has no host
    authorization model to apply -- the report is reachable regardless of client."""
    eid = _make_engagement(session_factory, client_id=ACME)
    assert client.get(f"{UI}/engagements/{eid}/report").status_code == 200


def test_a_host_missing_the_capability_fails_closed(client, stub_host, session_factory):
    """The regression guard for the defect itself.

    A host bundle that predates `can_view_client` must be REFUSED, not fall back to a local rule — this
    module holds no policy to fall back to. That is correct, and it is also exactly what made the shipped
    bug invisible: it looks identical to a working deny. Asserted here so that if the host ever stops
    injecting the key, one test says *why* every report went dark.
    """
    eid = _make_engagement(session_factory, client_id=ACME)
    stub_host.current_user = StubUser(id=1, username="admin3", role=_StubRole("admin"))
    del client.application.extensions["scribble"].extras["can_view_client"]
    assert client.get(f"{UI}/engagements/{eid}/report").status_code == 404
