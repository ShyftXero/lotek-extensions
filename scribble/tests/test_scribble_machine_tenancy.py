"""Engagement-level tenancy on the PAT/MACHINE blueprint (`scribble/api_pat.py`).

`tests/test_scribble_tenancy_gate.py` closed this class on the two COOKIE blueprints and left
`machine_bp` open, with a documented follow-up: its routes loaded an `Engagement` by id and acted on it
with no check that the token's grant covered that engagement's client. Job-level tenancy WAS checked
(`host.findings().get_job`/`get_finding` apply `user_can_view_job` internally), and that is precisely
what made the hole easy to miss — `promote-job` carefully authorized the SOURCE of the data and never
the DESTINATION it wrote to.

What is proven here:

1. `test_every_machine_route_is_classified` — the same "a new route can't slip through unclassified"
   property the cookie blueprints have, derived from `app.url_map`: every `scribble_machine` route either
   carries an `engagement_id` (and is then required, by the test below, to actually deny a non-member) or
   is declared tenant-free here, which is a claim about a LIBRARY-WIDE table and nothing else.
2. `test_every_engagement_scoped_machine_route_denies_a_foreign_client` — a real HTTP request per route
   as a token holding a grant under a DIFFERENT client, asserting 404 on every one.
3. Per-route DENY→ALLOW pairs for `add_finding`/`promote-job`, each also asserting that the denial wrote
   NOTHING (no finding rows, and — for promote — no `host.mark_job_promoted` callback).
4. The create route's body-supplied `client_id`: required when mounted, refused when foreign, accepted
   when granted, and accepted in either host id shape (int or UUID).

Red → green (one-time proof, recorded here rather than wired in as a toggle): with the two
`can_view_engagement` checks removed from `api_pat.py`, tests 2, 3 and the promote-job denial fail (201/
200 instead of 404, and `promoted_calls` non-empty); with the create route's `client_id` block removed,
test 4's DENY cases fail (201 instead of 400/404). Restoring them turns all of it green.
"""

from __future__ import annotations

import uuid

from flask import url_for

import scribble.models as fm
from tests.conftest import StubActor

M = "/scribble/machine"

# Scribble's own client PK is UUIDv7 since lotek#335. Where a test seeds `scribble_clients` and
# ALSO grants on the same id via the stub host, both halves must move together.
ACME = uuid.uuid7()          # the client the token under test holds a grant under
OTHER_CLIENT = uuid.uuid7()  # a client it does not

# Machine routes with no engagement axis AT ALL: the vulnerability-template library and the VulnMap that
# indexes it are single, shared, tenant-free tables (the same reason `library_ui.py`'s routes carry no
# engagement id either). Anything NOT in here must be engagement-scoped and must deny a foreign client.
_TENANT_FREE_ENDPOINTS = frozenset(
    {
        "scribble_machine.scribble_list_templates",
        "scribble_machine.scribble_get_template",
        "scribble_machine.scribble_create_vuln_map",
        "scribble_machine.scribble_list_vuln_map",
        "scribble_machine.scribble_resolve_template",
        # Create takes its client from the BODY, so it has no view arg to classify by; its own tenancy
        # rule is proven by the four `test_create_engagement_*` cases below.
        "scribble_machine.scribble_create_engagement",
        # Authoring a template WRITES to that same library-wide table. It carries no engagement and no
        # client: a template is a reusable vuln description ("Weak TLS configuration"), never client data.
        # Classified with its sibling read routes above for exactly that reason.
        "scribble_machine.scribble_create_template",
    }
)

# Machine routes that legitimately answer 200 to ANY token because they are SCOPED LISTS: they return
# only the rows the caller may see, so the correct answer for a foreign token is an EMPTY list, not a 404.
# A 404 here would be wrong (the collection exists for everyone) and a blanket tenant-free exemption would
# be a lie (these rows ARE client data). So they get their own category with a STRONGER obligation, proven
# by `test_scoped_list_routes_return_nothing_to_a_foreign_client`: 200, and zero rows.
_SCOPED_LIST_ENDPOINTS = frozenset(
    {
        "scribble_machine.scribble_list_engagements",
    }
)


def _machine_rules(app):
    return sorted(
        (r for r in app.url_map.iter_rules() if r.endpoint.startswith("scribble_machine.")),
        key=lambda r: r.endpoint,
    )


def _build_url(app, rule, *, engagement_id: int, job_id: str) -> str:
    """Build a real URL for a machine rule via `url_for`.

    Deliberately NOT string substitution on `rule.rule`: an unsubstituted placeholder (the day a
    converter changes to `<uuid:engagement_id>`, say) would 404 at Werkzeug's routing layer, and a
    denial sweep asserting 404 would keep passing while testing nothing at all. `url_for` raises
    instead, and `_http_methods`' companion ALLOW sweep would fail too.
    """
    values = {}
    for arg in rule.arguments:
        if arg == "engagement_id":
            values[arg] = engagement_id
        elif arg == "job_id":
            values[arg] = job_id
        else:  # pragma: no cover - fails loudly rather than guessing a value
            raise AssertionError(f"{rule.endpoint} has an unrecognized view arg {arg!r}")
    with app.test_request_context():
        return url_for(rule.endpoint, **values)


def _http_methods(rule) -> list[str]:
    return sorted((rule.methods or set()) - {"HEAD", "OPTIONS"})


def _foreign_token(stub_host) -> None:
    """A perfectly valid `write`-scoped token — for a DIFFERENT client. This is the whole threat model:
    not an unauthenticated stranger, but a legitimate tool of tenant B reaching tenant A's data."""
    stub_host.actor = StubActor(id=8, username="tool-b", role="operator")
    stub_host.viewable_client_ids = {OTHER_CLIENT}


def _granted_token(stub_host) -> None:
    stub_host.actor = StubActor(id=7, username="tool-a", role="operator")
    stub_host.viewable_client_ids = {ACME}


def _engagement(session_factory, *, client_id) -> int:
    with session_factory() as db:
        eng = fm.Engagement(name="Machine target", client_id=client_id)
        db.add(eng)
        db.commit()
        return eng.id


def _template(session_factory) -> int:
    with session_factory() as db:
        tmpl = fm.VulnerabilityTemplate(name="Machine target template", content_json={}, content_html={})
        db.add(tmpl)
        db.commit()
        return tmpl.id


# ── 1. every machine route is classified ─────────────────────────────────────────────────────────────


def test_every_machine_route_is_classified(app):
    """A brand-new machine route must be either engagement-scoped (and therefore covered by the denial
    sweep below) or an explicit, argued claim that it touches no tenant data. Neither by default."""
    unclassified = [
        (rule.endpoint, rule.rule, sorted(rule.arguments))
        for rule in _machine_rules(app)
        if rule.endpoint not in _TENANT_FREE_ENDPOINTS
        and rule.endpoint not in _SCOPED_LIST_ENDPOINTS
        and "engagement_id" not in rule.arguments
    ]
    assert unclassified == [], (
        "machine route(s) neither engagement-scoped, scoped-list, nor declared tenant-free — classify "
        "them (add the engagement_id check in api_pat.py, or justify the addition to "
        f"_TENANT_FREE_ENDPOINTS / _SCOPED_LIST_ENDPOINTS here): {unclassified}"
    )


# ── 2. every engagement-scoped machine route denies a foreign client ─────────────────────────────────


def test_every_engagement_scoped_machine_route_denies_a_foreign_client(app, stub_host, session_factory):
    _foreign_token(stub_host)
    stub_host.findings.add_job("job-1", owner_id=8, dtos=[])  # the token's OWN job: source side is fine
    eid = _engagement(session_factory, client_id=ACME)
    client = app.test_client()

    allowed = []
    for rule in _machine_rules(app):
        if rule.endpoint in _TENANT_FREE_ENDPOINTS or rule.endpoint in _SCOPED_LIST_ENDPOINTS:
            continue
        url = _build_url(app, rule, engagement_id=eid, job_id="job-1")
        for method in _http_methods(rule):
            resp = client.open(url, method=method, json={})
            if resp.status_code != 404:
                allowed.append((rule.endpoint, method, url, resp.status_code))

    assert allowed == [], f"a token for another client was NOT denied on: {allowed}"


def test_scoped_list_routes_return_nothing_to_a_foreign_client(app, stub_host, session_factory):
    """The obligation a `_SCOPED_LIST_ENDPOINTS` entry buys instead of a 404: the collection answers 200
    to anyone, but a token for another client must see ZERO rows.

    This is the strictly harder property. A route exempted as "tenant-free" is never checked again; a
    scoped list is checked EVERY run against a populated table, so the day someone drops the
    `visible_engagements` filter — the one line between "your engagements" and "every client's
    engagements" — this goes red. An empty database would make it pass vacuously, so the fixture seeds a
    real engagement under the OTHER client first.
    """
    _engagement(session_factory, client_id=ACME)  # a row that exists and MUST NOT be disclosed
    _foreign_token(stub_host)
    client = app.test_client()

    for rule in _machine_rules(app):
        if rule.endpoint not in _SCOPED_LIST_ENDPOINTS:
            continue
        url = _build_url(app, rule, engagement_id=1, job_id="job-1")
        resp = client.get(url)
        assert resp.status_code == 200, (rule.endpoint, resp.status_code)
        body = resp.get_json()
        assert body["count"] == 0, f"{rule.endpoint} disclosed another client's rows: {body}"
        assert body["items"] == [], f"{rule.endpoint} disclosed another client's rows: {body}"


def test_scoped_list_routes_do_return_the_callers_own_rows(app, stub_host, session_factory):
    """The positive control for the sweep above — without it, a route that returned an empty list to
    EVERYONE (or 200 with nothing at all) would satisfy the denial test while being entirely broken."""
    _engagement(session_factory, client_id=ACME)
    _granted_token(stub_host)
    client = app.test_client()

    for rule in _machine_rules(app):
        if rule.endpoint not in _SCOPED_LIST_ENDPOINTS:
            continue
        resp = client.get(_build_url(app, rule, engagement_id=1, job_id="job-1"))
        assert resp.status_code == 200, (rule.endpoint, resp.status_code)
        assert resp.get_json()["count"] >= 1, f"{rule.endpoint} hid the caller's OWN rows"


def test_every_engagement_scoped_machine_route_allows_a_granted_token(app, stub_host, session_factory):
    """The companion positive, and the reason the sweep above can be trusted: the SAME urls, with a
    token that does hold the grant, must never come back 404. Without this, a sweep asserting "404
    everywhere" would also pass if every url were malformed, or if the machine blueprint stopped being
    mounted at all — the classic way a denial test decays into a test of nothing."""
    _granted_token(stub_host)
    stub_host.findings.add_job("job-1", owner_id=7, dtos=[])
    eid = _engagement(session_factory, client_id=ACME)
    client = app.test_client()

    denied = []
    for rule in _machine_rules(app):
        if rule.endpoint in _TENANT_FREE_ENDPOINTS:
            continue
        url = _build_url(app, rule, engagement_id=eid, job_id="job-1")
        for method in _http_methods(rule):
            # An empty body is a business 400 on add-finding; only 404 (the tenancy refusal) is a failure.
            resp = client.open(url, method=method, json={})
            if resp.status_code == 404:
                denied.append((rule.endpoint, method, url))

    assert denied == [], f"a granted token was WRONGLY denied on: {denied}"


# ── 3. add-finding ───────────────────────────────────────────────────────────────────────────────────


def test_add_finding_denied_for_another_clients_engagement(client, stub_host, session_factory):
    eid = _engagement(session_factory, client_id=ACME)
    tid = _template(session_factory)
    _foreign_token(stub_host)

    resp = client.post(f"{M}/engagements/{eid}/findings", json={"template_id": tid})
    assert resp.status_code == 404
    assert resp.get_json()["detail"] == "engagement not found"  # identical to a nonexistent id
    with session_factory() as db:
        assert db.get(fm.Engagement, eid).findings == []  # nothing authored


def test_add_finding_allowed_for_granted_client(client, stub_host, session_factory):
    eid = _engagement(session_factory, client_id=ACME)
    tid = _template(session_factory)
    _granted_token(stub_host)

    resp = client.post(f"{M}/engagements/{eid}/findings", json={"template_id": tid})
    assert resp.status_code == 201
    with session_factory() as db:
        assert len(db.get(fm.Engagement, eid).findings) == 1


def test_add_finding_refusal_is_identical_for_foreign_and_missing(client, stub_host, session_factory):
    """No existence oracle: a foreign engagement and one that was never created must be the same answer,
    byte for byte, or the id space is enumerable."""
    eid = _engagement(session_factory, client_id=ACME)
    tid = _template(session_factory)
    _foreign_token(stub_host)

    foreign = client.post(f"{M}/engagements/{eid}/findings", json={"template_id": tid})
    missing = client.post(f"{M}/engagements/999999/findings", json={"template_id": tid})
    assert (foreign.status_code, foreign.get_json()) == (missing.status_code, missing.get_json())


def test_add_finding_authorizes_before_it_validates_the_body(client, stub_host, session_factory):
    """A foreign engagement 404s whatever the body says — the tenancy check runs first. Otherwise the
    400/404 split would itself answer "does this engagement exist?" for every id probed."""
    eid = _engagement(session_factory, client_id=ACME)
    _foreign_token(stub_host)
    assert client.post(f"{M}/engagements/{eid}/findings", json={}).status_code == 404


# ── 4. promote-job (the route that spans two tenancy domains) ────────────────────────────────────────


def test_promote_job_denied_when_destination_belongs_to_another_client(client, stub_host, session_factory):
    """The token owns the SOURCE job outright — this is exactly the case the old code let through, since
    the only check it made was the one that passes here."""
    from tests.conftest import FakeFindingDTO

    _foreign_token(stub_host)
    stub_host.findings.add_job("job-1", owner_id=8, dtos=[FakeFindingDTO(id=1, title="SQLi")])
    eid = _engagement(session_factory, client_id=ACME)

    resp = client.post(f"{M}/engagements/{eid}/promote-job/job-1")
    assert resp.status_code == 404
    with session_factory() as db:
        assert db.get(fm.Engagement, eid).findings == []
    assert stub_host.promoted_calls == []  # the host-side write must not happen either


def test_promote_job_allowed_for_granted_client(client, stub_host, session_factory):
    from tests.conftest import FakeFindingDTO

    _granted_token(stub_host)
    stub_host.findings.add_job("job-1", owner_id=7, dtos=[FakeFindingDTO(id=1, title="SQLi")])
    eid = _engagement(session_factory, client_id=ACME)

    resp = client.post(f"{M}/engagements/{eid}/promote-job/job-1")
    assert resp.status_code == 200
    assert resp.get_json()["promoted"] == 1
    assert stub_host.promoted_calls == [("job-1", stub_host.actor, "scribble", eid)]


# ── 5. create: the client_id comes from the body ─────────────────────────────────────────────────────


def _created_engagement_count(session_factory) -> int:
    with session_factory() as db:
        return len(db.query(fm.Engagement).all())


def test_create_engagement_requires_a_client_when_mounted(client, stub_host, session_factory):
    """A client-less engagement is denied to everyone by the host's own `can_view_client(None, …)`, so
    creating one is a 201 for something readable and writable by nobody."""
    _granted_token(stub_host)
    resp = client.post(f"{M}/engagements", json={"name": "No client"})
    assert resp.status_code == 400
    assert resp.get_json()["error"] == "bad_request"
    assert _created_engagement_count(session_factory) == 0


def test_create_engagement_refuses_a_client_the_token_cannot_view(client, stub_host, session_factory):
    _granted_token(stub_host)
    resp = client.post(f"{M}/engagements", json={"name": "Theirs", "client_id": OTHER_CLIENT})
    assert resp.status_code == 404  # not 403: don't confirm which client ids exist
    assert _created_engagement_count(session_factory) == 0


def test_create_engagement_allows_a_granted_client(client, stub_host, session_factory):
    _granted_token(stub_host)
    resp = client.post(f"{M}/engagements", json={"name": "Ours", "client_id": ACME})
    assert resp.status_code == 201
    with session_factory() as db:
        assert db.get(fm.Engagement, resp.get_json()["id"]).client_id == ACME


def test_create_engagement_accepts_a_uuid_client_id(client, stub_host, session_factory):
    """`client_id` was parsed with `_opt_int`, so under a v2 host — whose client PKs are UUIDv7 — a
    machine caller could not pass a client at all, which is why every machine engagement was client-less
    in the first place. Gating a field nobody can set would be a route that can only fail."""
    client_uuid = uuid.UUID("0198a1b2-c3d4-7e5f-8a9b-0c1d2e3f4a5b")
    stub_host.actor = StubActor(id=7, username="tool-a", role="operator")
    stub_host.viewable_client_ids = {client_uuid}

    resp = client.post(f"{M}/engagements", json={"name": "v2 client", "client_id": str(client_uuid)})
    assert resp.status_code == 201
    with session_factory() as db:
        assert db.get(fm.Engagement, resp.get_json()["id"]).client_id == client_uuid


def test_create_engagement_rejects_an_unparseable_client_id(client, stub_host):
    _granted_token(stub_host)
    resp = client.post(f"{M}/engagements", json={"name": "E", "client_id": "not-an-id"})
    assert resp.status_code == 400
    assert resp.get_json()["error"] == "bad_request"
