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
from scribble.host import SCOPE_ATTR
from tests.conftest import StubActor, install_scope_enforcing_gate

_MISSING_ID = uuid.uuid7()  # a well-formed id that is not in the table

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
        # The published OpenAPI document (#116). It describes the SHAPE of this API — route templates,
        # scopes, request and response schemas — and reads no tenant row of any kind: it is generated
        # from `app.url_map` and a static schema table, never from the database. It is still `read`-scoped
        # (a token is required), so it discloses nothing to an unauthenticated caller either.
        "scribble_machine.scribble_machine_openapi",
    }
)

# View-arg names that identify an engagement INDIRECTLY, via a child row that belongs to exactly one
# engagement. The findings-CRUD routes (ext#41) are keyed on a finding id rather than an engagement id —
# `PATCH /findings/<fid>`, `DELETE /findings/<fid>`, `POST /findings/<fid>/move` — mirroring the cookie
# routes they replace, and they resolve tenancy by loading the child and following it to its engagement
# (`api_pat._visible_finding`). That makes them engagement-scoped in substance while carrying no
# `engagement_id` in the URL, so the classification test below has to recognize this shape or it would
# either fail closed on a correctly-authorized route or (worse, if someone "fixed" it by exempting them)
# leave the whole findings surface out of the denial sweep.
#
# The obligation is the SAME as for an `engagement_id` route: the sweeps seed these children under the
# ACME engagement and require a 404 for a token holding another client, and a non-404 for a granted one.
# This mirrors `authz._CHILD_RESOLVERS`, which does the same job for the cookie blueprints' gate.
_CHILD_ID_ARGS: tuple[str, ...] = ("finding_id", "group_id")

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


def _build_url(
    app,
    rule,
    *,
    engagement_id: int,
    job_id: str,
    artifact_id: int = 0,
    finding_id: int = 0,
    group_id: int = 0,
    attack_path_id: int = 0,
    template_id: int = 0,
) -> str:
    """Build a real URL for a machine rule via `url_for`.

    Deliberately NOT string substitution on `rule.rule`: an unsubstituted placeholder (the day a
    converter changes to `<uuid:engagement_id>`, say) would 404 at Werkzeug's routing layer, and a
    denial sweep asserting 404 would keep passing while testing nothing at all. `url_for` raises
    instead, and `_http_methods`' companion ALLOW sweep would fail too.

    Every child id (`finding_id`, `group_id`, `artifact_id`, `attack_path_id`, `template_id`) defaults to
    0 — an id no row ever has — rather than being an optional keyword a caller can forget: a sweep that
    silently built a URL with a MISSING child would be asserting 404 on a route that 404s for the wrong
    reason. The sweeps below pass real, seeded ids. A new id-carrying route adds a branch below AND a
    parameter here; the `else` raises rather than guessing, so it cannot be skipped by accident.
    """
    values = {}
    for arg in rule.arguments:
        if arg == "engagement_id":
            values[arg] = engagement_id
        elif arg == "job_id":
            values[arg] = job_id
        elif arg == "artifact_id":
            # A REAL artifact on that engagement, seeded by the caller. A made-up id would answer 404
            # "artifact not found" — which would make the DENY sweep pass for the wrong reason (not
            # tenancy) and the ALLOW sweep fail for the wrong reason.
            values[arg] = artifact_id
        elif arg == "finding_id":
            values[arg] = finding_id
        elif arg == "group_id":
            values[arg] = group_id
        elif arg == "attack_path_id":
            # A REAL diagram on that engagement, seeded fresh per request by the caller — same reason as
            # `artifact_id` above, plus one specific to this trio: the sweeps walk rules sorted by
            # endpoint name, so `scribble_delete_attack_path` runs BEFORE `..._get_...`/`..._update_...`
            # and a shared row would be gone by the time they asked for it.
            values[arg] = attack_path_id
        elif arg == "template_id":
            # Only the TENANT-FREE library routes carry this, so the two tenancy sweeps below never reach
            # it — the write sweep does, because the write axis applies to the shared library too.
            values[arg] = template_id
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


def _artifact_on(session_factory, engagement_id: int) -> int:
    """A real artifact row on ``engagement_id`` — see ``_build_url``'s ``artifact_id`` branch."""
    with session_factory() as db:
        art = fm.Artifact(
            engagement_id=engagement_id, filename="evidence.png", content_type="image/png",
            storage_path=f"{engagement_id}/evidence.png", byte_size=4,
        )
        db.add(art)
        db.commit()
        return art.id


def _diagram_on(session_factory, engagement_id: int):
    """A real linked attack path on ``engagement_id``, for the per-item routes added in #114.

    Fresh per request for the same reason as `_children`: one of the three routes is a DELETE."""
    with session_factory() as db:
        diagram = fm.EngagementDiagram(
            engagement_id=engagement_id, diagram_ref="d", embed_html="<html></html>", order_index=0
        )
        db.add(diagram)
        db.commit()
        return diagram.id


def _children(session_factory, engagement_id: int) -> tuple[int, int]:
    """Seed a FRESH finding + group under `engagement_id` and return their ids.

    Fresh per request, not once per test: the sweep exercises `DELETE /findings/<id>` and
    `DELETE /engagements/<id>/groups/<id>` too, and a shared row would be gone by the time the next
    route in the sweep asked for it — turning every later assertion into an accidental 404 and hiding
    whatever those routes actually do.
    """
    with session_factory() as db:
        group = fm.FindingGroup(engagement_id=engagement_id, name="Sweep section", order_index=0)
        db.add(group)
        db.flush()
        finding = fm.EngagementFinding(
            engagement_id=engagement_id, group_id=group.id, title="Sweep finding", order_index=0
        )
        db.add(finding)
        db.commit()
        return finding.id, group.id


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
        and not (set(rule.arguments) & set(_CHILD_ID_ARGS))
    ]
    assert unclassified == [], (
        "machine route(s) neither engagement-scoped (directly or via a child id), scoped-list, nor "
        "declared tenant-free — classify them (add the engagement/child tenancy check in api_pat.py, or "
        "justify the addition to _TENANT_FREE_ENDPOINTS / _SCOPED_LIST_ENDPOINTS here): "
        f"{unclassified}"
    )


def test_every_machine_route_id_converter_is_BOUNDED(app):
    """A path id must not be able to overflow the column it is about to be looked up by.

    Werkzeug's bare ``<int:x>`` is unbounded — a bare digit regex, ``num_convert=int``, no max — so a 30-digit
    id ROUTED successfully and then raised inside ``db.get()``: measured 500 on
    ``GET /scribble/machine/engagements/<10**30>`` and on its ``/artifacts`` sibling. On Postgres the
    equivalent ``DataError`` also poisons the open transaction. It is the same defect as an out-of-range
    ``finding_id`` in the request BODY (``_finding_id_or_400``), reached through the path instead.

    Bounding it in the converter is what makes the fix structural: Werkzeug does not match the rule at all,
    so the answer is a routing 404 — which is also the correct answer for "no such id" — and no view runs.
    This walks the live ``url_map`` so a route added later with a bare ``<int:>`` fails HERE rather than in
    production, which is the only reason a rule expressed as a shared ``_ID`` string constant can be
    trusted to stay applied."""
    from werkzeug.routing.converters import NumberConverter

    unbounded = []
    for rule in _machine_rules(app):
        for arg, conv in (rule._converters or {}).items():
            if isinstance(conv, NumberConverter) and (conv.max is None or conv.max > 2**31 - 1):
                unbounded.append((rule.endpoint, arg, conv.max))
    assert unbounded == [], (
        "machine route(s) with an unbounded integer id converter — use api_pat._ID, or a 30-digit path "
        f"segment 500s inside db.get(): {unbounded}"
    )


# ── 2. every engagement-scoped machine route denies a foreign client ─────────────────────────────────


def test_every_engagement_scoped_machine_route_denies_a_foreign_client(app, stub_host, session_factory):
    _foreign_token(stub_host)
    stub_host.findings.add_job("job-1", owner_id=8, dtos=[])  # the token's OWN job: source side is fine
    eid = _engagement(session_factory, client_id=ACME)
    aid = _artifact_on(session_factory, eid)
    client = app.test_client()

    allowed = []
    for rule in _machine_rules(app):
        if rule.endpoint in _TENANT_FREE_ENDPOINTS or rule.endpoint in _SCOPED_LIST_ENDPOINTS:
            continue
        for method in _http_methods(rule):
            # Real child rows, under the ACME engagement, per request — so a `finding_id`/`group_id`
            # route is denied because of TENANCY and not because the id happened not to exist.
            fid, gid = _children(session_factory, eid)
            url = _build_url(
                app,
                rule,
                engagement_id=eid,
                job_id="job-1",
                artifact_id=aid,
                finding_id=fid,
                group_id=gid,
                attack_path_id=_diagram_on(session_factory, eid),
            )
            resp = client.open(url, method=method, json={"finding_ids": [fid], "group_id": gid})
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
    aid = _artifact_on(session_factory, eid)
    client = app.test_client()

    denied = []
    for rule in _machine_rules(app):
        if rule.endpoint in _TENANT_FREE_ENDPOINTS:
            continue
        for method in _http_methods(rule):
            # Fresh children per request: this sweep really does exercise the two DELETE routes, so a
            # shared finding/group would be gone for every rule sorted after them and every later
            # assertion would pass for the wrong reason (see `_children`).
            fid, gid = _children(session_factory, eid)
            url = _build_url(
                app,
                rule,
                engagement_id=eid,
                job_id="job-1",
                artifact_id=aid,
                finding_id=fid,
                group_id=gid,
                attack_path_id=_diagram_on(session_factory, eid),
            )
            # An empty body is a business 400 on add-finding; only 404 (the tenancy refusal) is a failure.
            resp = client.open(url, method=method, json={})
            if resp.status_code == 404:
                denied.append((rule.endpoint, method, url, resp.get_json()))

    assert denied == [], f"a granted token was WRONGLY denied on: {denied}"


# ── 2b. the WRITE axis: a read-only token may look, never mutate ─────────────────────────────────────

# POST routes that are QUERIES, not mutations: they carry a lookup in the body because the key is too
# structured for a query string, write nothing, and are correctly `read`-scoped. The HTTP method alone
# cannot tell them from a mutation, so the sweep below needs them named — as a RATCHET, seeded with what
# already shipped this way. Adding an entry is a claim that the route writes nothing; the positive
# control below refuses an entry that is `write`-scoped, so a genuine mutator cannot be parked here to
# silence the sweep.
_READ_ONLY_POST_ENDPOINTS = frozenset(
    {
        # Resolves a vuln-map rule to a template id and returns it — one `select`, no `commit`.
        "scribble_machine.scribble_resolve_template",
    }
)



def test_every_mutating_machine_route_refuses_a_read_only_token(app, stub_host, session_factory):
    """A token that MAY view this engagement but holds only the ``read`` scope must not mutate anything.

    The machine-surface counterpart of `test_scribble_tenancy_gate`'s write sweep (#131). That gate is a
    ``before_request`` on the two COOKIE blueprints and is deliberately NOT attached to ``machine_bp``
    (it resolves the browser-session actor, which is None on a PAT request, so it would 404 every machine
    route). The machine surface's write axis is `@host.require_scope("write")` applied PER ROUTE — which
    is precisely the shape that rots: a new mutator ships ungated the day someone forgets the decorator,
    and under the conftest's no-op ``require_pat_scope`` it still looks perfectly gated.

    So this asserts the CLASS, not a list. It is driven off ``app.url_map``, so `PATCH`/`DELETE`
    ``…/attack-paths/<id>`` (added here for #114) — and anything added after them — are covered without
    being named. `test_machine_findings_crud`'s `_WRITE_ROUTE_CALLS` pair proves the same property
    against eight hand-written URLs with real bodies; it is the readable per-route control, this is the
    one that cannot go stale.

    Red → green: dropping `@host.require_scope("write")` from `scribble_update_attack_path` and
    `scribble_delete_attack_path` turns their 403s into 200/business-4xx and this fails naming both
    (the DELETE answers 200 — a read-only token really does destroy the row); restoring the decorators is
    green.

    Honest limit, per this repo's own rule: a stub proves logic, never the mount. What is asserted here is
    that each route DECLARES the right scope and that the declaration is load-bearing — the real gate is
    lotek's `api_v1.require_scope`, reached through `host_contract.require_pat_scope`, and only a MOUNTED
    test in core (`tests/test_scribble_extension.py`) proves that wiring.
    """
    install_scope_enforcing_gate(app, stub_host)
    stub_host.actor = StubActor(
        id=31, username="read-only-tool", role="operator", scopes=frozenset({"read"})
    )
    stub_host.viewable_client_ids = {ACME}  # it MAY see this client — only the write axis refuses
    stub_host.findings.add_job("job-1", owner_id=31, dtos=[])
    eid = _engagement(session_factory, client_id=ACME)
    aid = _artifact_on(session_factory, eid)
    tid = _template(session_factory)
    client = app.test_client()

    mutated, view_refused = [], []
    for rule in _machine_rules(app):
        if rule.endpoint in _READ_ONLY_POST_ENDPOINTS:
            continue
        for method in _http_methods(rule):
            # Fresh children per request, for the same reason the sweeps above do it: this walks the
            # DELETE routes too, and a 403 today is no guarantee a future regression won't let one land.
            fid, gid = _children(session_factory, eid)
            url = _build_url(
                app,
                rule,
                engagement_id=eid,
                job_id="job-1",
                artifact_id=aid,
                finding_id=fid,
                group_id=gid,
                attack_path_id=_diagram_on(session_factory, eid),
                template_id=tid,
            )
            resp = client.open(url, method=method, json={})
            if method == "GET":
                # The positive control, and not a formality: without it a blanket 403 (a broken gate that
                # refused everything, or a token with no scopes at all) would satisfy the sweep above.
                if resp.status_code == 403:
                    view_refused.append((rule.endpoint, url, resp.get_json()))
            elif resp.status_code != 403:
                mutated.append((rule.endpoint, method, url, resp.status_code))

    assert mutated == [], (
        f"a read-only token was NOT refused (403) on mutating machine route(s): {mutated}. "
        "Every mutating route on `machine_bp` needs `@host.require_scope(\"write\")`."
    )
    assert view_refused == [], f"the write axis wrongly refused a READ route: {view_refused}"

    # The ratchet's positive control: every exemption must really be a `read`-scoped route, so the set
    # cannot be padded with a mutator instead of the route being fixed.
    for endpoint in _READ_ONLY_POST_ENDPOINTS:
        rule = next(r for r in _machine_rules(app) if r.endpoint == endpoint)
        view = app.view_functions[endpoint]
        assert getattr(view, SCOPE_ATTR, None) == "read", (
            f"{endpoint} is exempted from the write sweep but is not read-scoped — fix the route, "
            "do not extend _READ_ONLY_POST_ENDPOINTS"
        )
        assert "POST" in (rule.methods or set()), f"{endpoint} is exempted but takes no POST"


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
    missing = client.post(f"{M}/engagements/{_MISSING_ID}/findings", json={"template_id": tid})
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
