"""Coverage guard for the blueprint-wide tenancy gate (`scribble/authz.py`).

This is the regression guard the systemic gap itself proves the need for: before this gate existed, ONLY
the report routes (`report_html_api.py`/`report_docx_api.py`) called `authorize_engagement_view` -- every
other engagement-scoped route on `bp`/`api_bp` (`engagement_ui.py`'s board/edit/delete/add-finding/
reorder, `artifacts_api.py`'s artifact CRUD, `checklists_api.py`'s assignment routes, `autosave_api.py`/
`collab/*`'s per-block routes) did a bare `db.get(...)` with no tenancy check at all -- any authenticated
actor could read AND write another client's engagement data by walking ids.

Three things below make that gap hard to reopen:

1. `test_every_scribble_route_is_classified` -- derives the FULL route list from `app.url_map` and
   asserts every route is either (a) declared non-engagement-scoped in `_NON_SCOPED_ENDPOINTS`
   (dashboard/list/create, or a route on a library-wide table -- `VulnerabilityTemplate`/
   `ChecklistTemplate`/`AssessmentType` -- that carries no engagement axis at all) or (b) carries a
   view-arg name `scribble.authz` already knows how to resolve to an engagement. A brand-new route with
   a brand-new id name that is neither FAILS this test -- forcing an explicit choice (extend the
   resolver map, or declare it non-scoped here) instead of silently shipping unguarded.
2. `test_every_scoped_route_denies_a_non_member` -- for every route classified as scoped, fires a REAL
   HTTP request as a wired-up non-member actor and asserts 404. This is the actual proof the gate is
   wired, not just "coverable in principle" -- see the module docstring's red/green note below.
3. `test_every_scoped_route_allows_a_member` -- the companion positive: the SAME routes, actor holding a
   real client grant, must NOT come back 404 -- proving the gate doesn't overblock a legitimate caller.
   (A business-logic 4xx from an intentionally-empty request body is fine and expected for some POST
   routes; only 404, the gate's specific refusal code, is asserted against.)

Plus explicit per-class ALLOW/DENY tests for the routes named in the audit (board/edit/delete/
add_finding/reorder/artifact_raw), in the same style as `test_scribble_report_authz.py`, and dedicated
coverage for the two BODY-scoped routes the view-arg gate structurally can't reach
(`create_artifact`/`templating_preview`), which got their own direct `authorize_engagement_view` call
instead -- see those modules' own comments.

Red -> green (recorded here since it's a one-time proof, not something worth wiring into CI as a
toggle): with `scribble.authz.register_gate`'s two `before_request` registrations commented out in
`scribble/__init__.py`, `test_every_scoped_route_denies_a_non_member` and every explicit DENY test below
fail (200/302 instead of 404) -- restoring the registration turns them green, with the rest of the suite
(and the ALLOW tests) unaffected either way, since the standalone/no-host and authorized-actor paths
never depended on it.
"""

from __future__ import annotations

import io

from flask import url_for

import scribble.models as fm
from scribble.artifacts_storage import save_bytes
from scribble.authz import _CHILD_RESOLVERS, _DIRECT_KEYS
from scribble.enums import ChecklistKind
from tests.conftest import StubUser, _StubRole

UI = "/scribble"
API = "/scribble/api"

ACME = 501          # the client under test
OTHER_CLIENT = 502  # a client the actor holds no grant under

# Derived from the gate itself (not duplicated by hand) -- see scribble/authz.py.
_RECOGNIZED_KEYS = frozenset(_DIRECT_KEYS) | frozenset(_CHILD_RESOLVERS)

# Routes this GATE does not scope, because they carry no engagement id in their URL. Read the list
# carefully: "no engagement view arg" is a fact about the URL, NOT a claim that the route touches no
# engagement data, and conflating the two is precisely how `dashboard`/`engagements`/`engagement_new`
# sat here while enumerating every tenant's engagements and accepting any client id from the form. They
# are still gate-exempt (nothing for a view-arg resolver to resolve) and are now scoped by their own
# means -- `authz.filter_visible_engagements` and `engagement_ui._resolve_client`, proven in
# `tests/test_scribble_list_tenancy.py`.
#
# The rest are genuinely tenant-free: every route on a library-wide table shared across all tenants
# (VulnerabilityTemplate/ChecklistTemplate/AssessmentType) rather than one engagement's data. Plus the
# two BODY-scoped routes fixed with a direct `authorize_engagement_view` call instead of this gate (the
# view-arg gate structurally can't reach a body field) -- see
# `test_artifact_upload_to_foreign_engagement_denied` /`test_templating_preview_denies_foreign_engagement`
# below for their own coverage.
_NON_SCOPED_ENDPOINTS = frozenset(
    {
        "scribble.static",
        "scribble.dashboard",
        "scribble.engagements",
        "scribble.engagement_new",
        "scribble.library",
        "scribble.library_new",
        "scribble.library_detail",
        "scribble.assessment_types",
        "scribble.checklists_library",
        "scribble_api.health",
        "scribble_api.create_template",
        "scribble_api.update_template",
        "scribble_api.duplicate_template",
        "scribble_api.delete_template",
        "scribble_api.save_template_block",
        "scribble_api.get_template_block",
        "scribble_api.create_assessment_type",
        "scribble_api.update_assessment_type",
        "scribble_api.delete_assessment_type",
        "scribble_api.list_checklist_templates",
        "scribble_api.suggest_checklist_templates",
        "scribble_api.create_checklist_template",
        "scribble_api.edit_checklist_template",
        "scribble_api.hide_checklist_template",
        "scribble_api.reset_checklist_template",
        "scribble_api.duplicate_checklist_template",
        "scribble_api.export_checklist_template",
        "scribble_api.create_artifact",  # body-scoped; direct call instead -- see artifacts_api.py
        "scribble_api.templating_preview",  # body-scoped; direct call instead -- see templating_api.py
    }
)

# The real websocket route can't be exercised through the plain Flask test client: a plain request
# without a websocket Upgrade header 400s at Werkzeug's routing layer (WebsocketMismatch) before it
# would ever reach a before_request hook -- verified empirically, and the same reason
# `collab/crdt.py::collab_ws` itself is `# pragma: no cover - needs a real socket`. It shares its
# `finding_id` resolution with `collab_status` below (identical view arg, identical resolver), which IS
# exercised, so this is a gap in "real HTTP request" coverage, not in the gate's classification logic
# (it's still required to appear in `_RECOGNIZED_KEYS`, and does, via `finding_id`).
_UNTESTABLE_VIA_HTTP = frozenset({"scribble.collab_ws"})


def _scribble_rules(app):
    return sorted(
        (r for r in app.url_map.iter_rules() if r.endpoint.split(".")[0] in ("scribble", "scribble_api")),
        key=lambda r: r.endpoint,
    )


def _http_methods(rule) -> list[str]:
    return sorted((rule.methods or set()) - {"HEAD", "OPTIONS"})


def _make_tree(session_factory, app, client_id: int) -> dict[str, int]:
    """One fully-linked engagement + one child row of every kind the gate resolves, all under
    `client_id`. Returns ``{view_arg_name: id}``.

    Callers doing a MUTATING request (delete/unassign) must call this fresh per request -- the tree is
    not safe to share across a mutation and a later read of the same rows.
    """
    with session_factory() as db:
        eng = fm.Engagement(name="Tenancy Gate Target", client_id=client_id)
        db.add(eng)
        db.commit()

        tmpl = fm.VulnerabilityTemplate(name="Gate Target Template", content_json={}, content_html={})
        db.add(tmpl)
        db.commit()

        finding = fm.EngagementFinding.from_template(tmpl, engagement_id=eng.id, order_index=0)
        db.add(finding)
        group = fm.FindingGroup(engagement_id=eng.id, name="Group", order_index=0)
        db.add(group)
        db.commit()

        cfg = app.extensions["scribble"]
        storage_path, sha256, size = save_bytes(cfg, eng.id, "evidence.txt", b"evidence")
        artifact = fm.Artifact(
            engagement_id=eng.id,
            filename="evidence.txt",
            content_type="text/plain",
            storage_path=storage_path,
            byte_size=size,
            sha256=sha256,
        )
        db.add(artifact)

        checklist = fm.EngagementChecklist(
            engagement_id=eng.id, name="Checklist", kind=ChecklistKind.coverage
        )
        db.add(checklist)
        db.commit()
        item = fm.EngagementChecklistItem(engagement_checklist_id=checklist.id, text="Item one")
        db.add(item)
        db.commit()

        return {
            "engagement_id": eng.id,
            "eid": eng.id,
            "finding_id": finding.id,
            "group_id": group.id,
            "artifact_id": artifact.id,
            "cid": checklist.id,
            "iid": item.id,
        }


def _build_url(app, rule, ids: dict[str, int]) -> str:
    values = {}
    for arg in rule.arguments:
        if arg in ids:
            values[arg] = ids[arg]
        elif arg == "block":
            values[arg] = "description"
        else:  # pragma: no cover - fails loudly rather than guessing a value
            raise AssertionError(f"{rule.endpoint} has an unrecognized view arg {arg!r}")
    with app.test_request_context():
        return url_for(rule.endpoint, **values)


# ── 1. every route must be classified ────────────────────────────────────────────────────────────────


def test_every_scribble_route_is_classified(app):
    """Every `bp`/`api_bp` route is either declared non-scoped or carries a recognized engagement id.

    Fails on a brand-new route that introduces a brand-new id name (e.g. `variable_value_id`) -- exactly
    the class of change that must NOT be able to slip through unguarded and unnoticed.
    """
    unclassified = [
        (rule.endpoint, rule.rule, sorted(rule.arguments))
        for rule in _scribble_rules(app)
        if rule.endpoint not in _NON_SCOPED_ENDPOINTS and not (set(rule.arguments) & _RECOGNIZED_KEYS)
    ]
    assert unclassified == [], (
        "route(s) with no recognized engagement-scoping id and not declared non-scoped -- classify "
        "them (extend scribble.authz's _DIRECT_KEYS/_CHILD_RESOLVERS, or add to _NON_SCOPED_ENDPOINTS "
        f"in this file): {unclassified}"
    )


# ── 2. every scoped route denies a non-member ────────────────────────────────────────────────────────


def test_every_scoped_route_denies_a_non_member(app, stub_host, session_factory):
    stub_host.current_user = StubUser(id=91, username="outsider", role=_StubRole("operator"))
    stub_host.viewable_client_ids = set()  # holds no grant anywhere

    # Nothing here ever mutates (the gate blocks before any view function runs), so one tree is safe to
    # reuse across every rule/method.
    ids = _make_tree(session_factory, app, ACME)
    client = app.test_client()

    allowed_when_it_should_not_be = []
    for rule in _scribble_rules(app):
        if rule.endpoint in _NON_SCOPED_ENDPOINTS or rule.endpoint in _UNTESTABLE_VIA_HTTP:
            continue
        if not (set(rule.arguments) & _RECOGNIZED_KEYS):
            continue
        url = _build_url(app, rule, ids)
        for method in _http_methods(rule):
            resp = client.open(url, method=method)
            if resp.status_code != 404:
                allowed_when_it_should_not_be.append((rule.endpoint, method, url, resp.status_code))

    assert allowed_when_it_should_not_be == [], (
        f"non-member was NOT denied on: {allowed_when_it_should_not_be}"
    )


# ── 3. every scoped route still allows a member (no over-block) ─────────────────────────────────────


def test_every_scoped_route_allows_a_member(app, stub_host, session_factory):
    """The companion positive to #2: an authorized actor (admin, so this isn't re-testing the grant
    predicate itself -- that's `test_scribble_report_authz.py`'s job) must never see the GATE's 404. A
    business 4xx from an intentionally empty request body is fine; only 404 is a gate failure here."""
    stub_host.current_user = StubUser(id=1, username="admin-member", role=_StubRole("admin"))

    overblocked = []
    server_errors = []
    for rule in _scribble_rules(app):
        if rule.endpoint in _NON_SCOPED_ENDPOINTS or rule.endpoint in _UNTESTABLE_VIA_HTTP:
            continue
        if not (set(rule.arguments) & _RECOGNIZED_KEYS):
            continue
        # Fresh tree per rule: some of these routes are mutating (delete/unassign), and a rule earlier
        # in the loop must never corrupt a later rule's fixtures.
        ids = _make_tree(session_factory, app, ACME)
        url = _build_url(app, rule, ids)
        client = app.test_client()
        for method in _http_methods(rule):
            resp = client.open(url, method=method, json={})
            if resp.status_code == 404:
                overblocked.append((rule.endpoint, method, url))
            elif resp.status_code >= 500:
                # Flask's test client (TESTING not set on this app) turns an unhandled view exception
                # into a 500 response rather than propagating it -- an assertion that only checked for
                # 404 would silently pass through a masked crash here. Surfaced as its own category so
                # a future regression can't hide behind "well, it wasn't a 404".
                server_errors.append((rule.endpoint, method, url, resp.status_code))

    assert overblocked == [], f"authorized member was WRONGLY denied (404) on: {overblocked}"
    assert server_errors == [], f"authorized member hit a server error (not a gate failure): {server_errors}"


# ── explicit per-class DENY/ALLOW tests (the named routes from the audit) ───────────────────────────


def _make_engagement(session_factory, *, client_id) -> int:
    with session_factory() as db:
        eng = fm.Engagement(name="Named-route target", client_id=client_id)
        db.add(eng)
        db.commit()
        return eng.id


def _outsider(stub_host) -> None:
    stub_host.current_user = StubUser(id=61, username="outsider2", role=_StubRole("operator"))
    stub_host.viewable_client_ids = set()


def _member(stub_host) -> None:
    stub_host.current_user = StubUser(id=62, username="member2", role=_StubRole("operator"))
    stub_host.viewable_client_ids = {ACME}


def test_engagement_board_denied_then_allowed(client, stub_host, session_factory):
    eid = _make_engagement(session_factory, client_id=ACME)
    _outsider(stub_host)
    assert client.get(f"{UI}/engagements/{eid}").status_code == 404
    _member(stub_host)
    assert client.get(f"{UI}/engagements/{eid}").status_code == 200


def test_engagement_edit_denied_then_allowed(client, stub_host, session_factory):
    eid = _make_engagement(session_factory, client_id=ACME)
    _outsider(stub_host)
    assert client.get(f"{UI}/engagements/{eid}/edit").status_code == 404
    assert client.post(f"{UI}/engagements/{eid}/edit", data={"name": "x"}).status_code == 404
    _member(stub_host)
    assert client.get(f"{UI}/engagements/{eid}/edit").status_code == 200
    # The form must keep naming a client the member holds: an edit that drops it would leave the
    # engagement openable by nobody, and one that names a foreign client is a hand-off to another tenant
    # -- both refused by `engagement_ui._resolve_client` (see tests/test_scribble_list_tenancy.py).
    edit = client.post(f"{UI}/engagements/{eid}/edit", data={"name": "Renamed", "client_id": str(ACME)})
    assert edit.status_code == 302


def test_engagement_delete_denied_then_allowed(client, stub_host, session_factory):
    denied_eid = _make_engagement(session_factory, client_id=ACME)
    _outsider(stub_host)
    assert client.post(f"{UI}/engagements/{denied_eid}/delete").status_code == 404
    with session_factory() as db:
        assert db.get(fm.Engagement, denied_eid) is not None  # never touched

    allowed_eid = _make_engagement(session_factory, client_id=ACME)
    _member(stub_host)
    assert client.post(f"{UI}/engagements/{allowed_eid}/delete").status_code == 302
    with session_factory() as db:
        assert db.get(fm.Engagement, allowed_eid) is None


def test_add_finding_denied_then_allowed(client, stub_host, session_factory):
    eid = _make_engagement(session_factory, client_id=ACME)
    with session_factory() as db:
        tmpl = fm.VulnerabilityTemplate(name="T", content_json={}, content_html={})
        db.add(tmpl)
        db.commit()
        tmpl_id = tmpl.id

    _outsider(stub_host)
    resp = client.post(f"{UI}/engagements/{eid}/findings", data={"template_id": str(tmpl_id)})
    assert resp.status_code == 404
    with session_factory() as db:
        assert db.get(fm.Engagement, eid).findings == []  # never touched

    _member(stub_host)
    resp = client.post(f"{UI}/engagements/{eid}/findings", data={"template_id": str(tmpl_id)})
    assert resp.status_code == 302
    with session_factory() as db:
        assert len(db.get(fm.Engagement, eid).findings) == 1


def test_groups_reorder_denied_then_allowed(client, stub_host, session_factory):
    eid = _make_engagement(session_factory, client_id=ACME)
    with session_factory() as db:
        g = fm.FindingGroup(engagement_id=eid, name="G", order_index=0)
        db.add(g)
        db.commit()

    _outsider(stub_host)
    resp = client.post(f"{API}/engagements/{eid}/groups/reorder", json={"order": []})
    assert resp.status_code == 404

    _member(stub_host)
    resp = client.post(f"{API}/engagements/{eid}/groups/reorder", json={"order": []})
    assert resp.status_code == 200


def test_artifact_raw_denied_then_allowed(client, stub_host, session_factory, app):
    eid = _make_engagement(session_factory, client_id=ACME)
    cfg = app.extensions["scribble"]
    storage_path, sha256, size = save_bytes(cfg, eid, "evidence.png", b"pngbytes")
    with session_factory() as db:
        artifact = fm.Artifact(
            engagement_id=eid,
            filename="evidence.png",
            content_type="image/png",
            storage_path=storage_path,
            byte_size=size,
            sha256=sha256,
        )
        db.add(artifact)
        db.commit()
        aid = artifact.id

    _outsider(stub_host)
    assert client.get(f"{API}/artifacts/{aid}/raw").status_code == 404

    _member(stub_host)
    resp = client.get(f"{API}/artifacts/{aid}/raw")
    assert resp.status_code == 200
    assert resp.get_data() == b"pngbytes"


# ── the two body-scoped routes (direct authorize_engagement_view calls, not this gate) ──────────────


def test_artifact_upload_to_foreign_engagement_denied(client, stub_host, session_factory):
    """`POST /artifacts` takes its `engagement_id` from the body, not the URL -- the view-arg gate
    can't reach it, so `artifacts_api.create_artifact` calls `authorize_engagement_view` directly. This
    proves that call, and that it runs BEFORE any byte is written to disk."""
    eid = _make_engagement(session_factory, client_id=ACME)
    _outsider(stub_host)
    resp = client.post(
        f"{API}/artifacts",
        data={"engagement_id": str(eid), "file": (io.BytesIO(b"x"), "f.txt")},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 404
    with session_factory() as db:
        assert db.get(fm.Engagement, eid).artifacts == []  # nothing written


def test_artifact_upload_to_own_client_engagement_allowed(client, stub_host, session_factory):
    eid = _make_engagement(session_factory, client_id=ACME)
    _member(stub_host)
    resp = client.post(
        f"{API}/artifacts",
        data={"engagement_id": str(eid), "file": (io.BytesIO(b"x"), "f.txt")},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 201


def test_templating_preview_denies_foreign_engagement(client, stub_host, session_factory):
    """`POST /preview` is the same body-scoped shape -- same direct-call fix in `templating_api.py`."""
    eid = _make_engagement(session_factory, client_id=ACME)
    _outsider(stub_host)
    resp = client.post(f"{API}/preview", json={"engagement_id": eid, "text": "hello {{TARGET_HOST}}"})
    assert resp.status_code == 404


def test_templating_preview_allows_own_client_engagement(client, stub_host, session_factory):
    eid = _make_engagement(session_factory, client_id=ACME)
    _member(stub_host)
    resp = client.post(f"{API}/preview", json={"engagement_id": eid, "text": "hello"})
    assert resp.status_code == 200


# ── standalone must still work (no host bundle -> no tenancy model to apply) ────────────────────────


def test_standalone_board_and_artifact_raw_unaffected(client, session_factory, app):
    """No `stub_host` fixture here -- `cfg.extras['host']` is absent, so `authorize_engagement_view`
    (and therefore the gate) fails OPEN, exactly like the report routes already did. Mirrors
    `test_scribble_report_authz.py::test_standalone_no_host_applies_no_authorization`."""
    eid = _make_engagement(session_factory, client_id=ACME)
    assert client.get(f"{UI}/engagements/{eid}").status_code == 200

    cfg = app.extensions["scribble"]
    storage_path, sha256, size = save_bytes(cfg, eid, "e.txt", b"y")
    with session_factory() as db:
        artifact = fm.Artifact(
            engagement_id=eid, filename="e.txt", content_type="text/plain",
            storage_path=storage_path, byte_size=size, sha256=sha256,
        )
        db.add(artifact)
        db.commit()
        aid = artifact.id
    assert client.get(f"{API}/artifacts/{aid}/raw").status_code == 200
