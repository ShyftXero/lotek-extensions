"""Machine (PAT/Bearer) API — the AUTHORING surface added by the machine-authoring branch:
``scribble/api_pat.py`` mounted at ``/scribble/machine/*``.

Covers the routes/behaviours the earlier ``test_machine_engagements.py`` did NOT:

  * the THIRD add-finding branch — author a finding directly from the body (plain-text prose AND a
    ``content_json`` dict), including the load-bearing security property that a write-scoped PAT's
    ``content_json`` is SANITIZED end-to-end before persist (a stored-XSS gate);
  * ``POST /templates`` — author a reusable vuln template;
  * ``GET /engagements`` (scoped list) + ``GET /engagements/{id}`` (counts, and the no-existence-oracle
    404 for a foreign engagement);
  * ``GET /engagements/{id}/report?format=html|docx`` — stream the rendered deliverable;
  * ``@host.require_scope`` enforcement (read routes vs write routes) — proven with a REAL scope-checking
    host gate, not the conftest's no-op passthrough, so a route that declared the wrong scope (or none)
    would fail here.

Auth/scope RBAC is the HOST's own concern (proven against a real lotek host in the lotek repo); these are
scribble's OWN proofs against the ``stub_host`` fixture (see ``tests/conftest.py``).
"""

from __future__ import annotations

import functools

from flask import jsonify

import scribble.models as fm
from scribble.content import schema
from scribble.enums import Severity
from tests.conftest import StubActor

M = "/scribble/machine"

ACME = 501          # a client the (admin) fixture actor can always see
OTHER_CLIENT = 777  # a second client, for the list-scoping test


# ── helpers ──────────────────────────────────────────────────────────────────────────────────────────


def _make_engagement(session_factory, *, name: str = "E", client_id=ACME) -> int:
    """Create an engagement directly via the ORM (bypassing the create route), so authoring/report tests
    start from a known engagement the fixture's default ADMIN actor can view (admin sees every client)."""
    with session_factory() as db:
        eng = fm.Engagement(name=name, scope_type="external", client_id=client_id, company_name="Acme")
        db.add(eng)
        db.commit()
        return eng.id


def _finding_with_body(session_factory, engagement_id: int, title: str = "SMB signing", **kw) -> None:
    with session_factory() as db:
        finding = fm.EngagementFinding(
            engagement_id=engagement_id, title=title, severity=Severity.medium, order_index=0,
            content_json={"description": schema.doc_from_text("body")}, **kw,
        )
        db.add(finding)
        db.commit()


def _node_types(doc) -> set[str]:
    return {n.get("type") for n in schema.iter_nodes(doc)}


def _install_scope_enforcing_gate(app, stub_host) -> None:
    """Replace the conftest's NO-OP ``require_pat_scope`` with one that REALLY checks the PAT actor's
    scopes. Scope RBAC is the host's concern, but *which scope each route declares* is scribble's — and
    that is only provable if a read token is actually refused by a write route (a no-op stub makes every
    route look correctly gated even if its decorator were missing or named the wrong scope)."""
    cfg = app.extensions["scribble"]

    def require_pat_scope(scope: str):
        def decorator(fn):
            @functools.wraps(fn)
            def wrapper(*args, **kwargs):
                actor = stub_host.actor
                if actor is None or scope not in actor.scopes:
                    return jsonify({"error": "forbidden", "detail": f"scope {scope} required"}), 403
                return fn(*args, **kwargs)

            return wrapper

        return decorator

    cfg.extras["require_pat_scope"] = require_pat_scope


# ── author a finding: plain-text prose branch ────────────────────────────────────────────────────────


def test_author_finding_from_plain_text(client, stub_host, session_factory):
    """Neither ``template_id`` nor ``lotek_finding_id`` -> author directly. Plain-text description/
    remediation/references are wrapped into content blocks; title + severity land on the row."""
    eid = _make_engagement(session_factory)
    resp = client.post(
        f"{M}/engagements/{eid}/findings",
        json={
            "title": "Reflected XSS",
            "severity": "high",
            "description": "User input is reflected without encoding.",
            "remediation": "Context-encode all output.",
            "references": ["https://owasp.org/xss", "CWE-79"],
            "target_host": "app.acme.test",
            "target_port": 443,
        },
    )
    assert resp.status_code == 201, resp.get_json()
    fid = resp.get_json()["finding_id"]
    with session_factory() as db:
        f = db.get(fm.EngagementFinding, fid)
        assert f is not None and f.engagement_id == eid and f.template_id is None
        assert f.title == "Reflected XSS"
        assert f.severity is Severity.high
        assert f.target_host == "app.acme.test"
        assert f.target_port == "443"  # coerced to str on the column
        assert "User input is reflected" in schema.plain_text(f.content_json["description"])
        assert "Context-encode" in schema.plain_text(f.content_json["remediation"])
        refs_text = schema.plain_text(f.content_json["references"])
        assert "https://owasp.org/xss" in refs_text and "CWE-79" in refs_text


def test_author_finding_requires_title_and_severity(client, stub_host, session_factory):
    """The direct-author branch has nothing to inherit title/severity from, so both are required."""
    eid = _make_engagement(session_factory)
    # No title (and no template/lotek id) -> 400.
    assert client.post(f"{M}/engagements/{eid}/findings", json={"description": "x"}).status_code == 400
    # Title but no severity -> 400.
    assert client.post(f"{M}/engagements/{eid}/findings", json={"title": "T"}).status_code == 400
    # Title + bad severity -> 400.
    r = client.post(f"{M}/engagements/{eid}/findings", json={"title": "T", "severity": "spicy"})
    assert r.status_code == 400


# ── author a finding: content_json branch + the stored-XSS sanitizer, END-TO-END ─────────────────────


def test_author_finding_content_json_is_sanitized_end_to_end(client, stub_host, session_factory):
    """RED-then-GREEN, end-to-end through ``add_finding``: a write-scoped PAT POSTs a ``content_json``
    carrying a raw-HTML/script node; the persisted document must have that node GONE while a sibling
    paragraph survives. The test is meaningful (not vacuous) because it first asserts the INPUT really is
    dangerous — the same assertion, run against the stored output, is what would go RED if the route ever
    stopped sanitizing."""
    eid = _make_engagement(session_factory)
    malicious_description = {
        "type": "doc",
        "content": [
            # attacker-supplied raw-HTML node smuggling a <script> — NOT an allowlisted node type
            {"type": "html", "html": "<script>alert(document.cookie)</script>"},
            {"type": "paragraph", "content": [{"type": "text", "text": "safe para"}]},
        ],
    }

    # RED baseline: the payload we are about to send genuinely carries the dangerous node.
    assert "html" in _node_types(malicious_description)
    assert "script" in str(malicious_description)

    resp = client.post(
        f"{M}/engagements/{eid}/findings",
        json={
            "title": "Stored XSS attempt",
            "severity": "critical",
            "content_json": {"description": malicious_description},
        },
    )
    assert resp.status_code == 201, resp.get_json()
    fid = resp.get_json()["finding_id"]

    # GREEN: the stored document dropped the raw-HTML node (and its script) but kept the legit paragraph.
    with session_factory() as db:
        stored = db.get(fm.EngagementFinding, fid).content_json["description"]
    types = _node_types(stored)
    assert "html" not in types and "script" not in types
    assert "script" not in str(stored)
    assert schema.plain_text(stored) == "safe para"


# ── POST /templates — author a reusable vuln template ────────────────────────────────────────────────


def test_create_template_persists_severity_refs_and_sanitized_content(client, stub_host, session_factory):
    resp = client.post(
        f"{M}/templates",
        json={
            "name": "Missing HSTS",
            "category": "web",
            "default_severity": "low",
            "description": "The Strict-Transport-Security header is absent.",
            "remediation": "Add HSTS with a long max-age.",
            "references": ["https://owasp.org/hsts"],
        },
    )
    assert resp.status_code == 201, resp.get_json()
    tid = resp.get_json()["id"]
    with session_factory() as db:
        t = db.get(fm.VulnerabilityTemplate, tid)
        assert t is not None and t.name == "Missing HSTS" and t.category == "web"
        assert t.default_severity is Severity.low
        assert t.active is True
        assert t.references == ["https://owasp.org/hsts"]  # references live on the column, not a block
        assert "Strict-Transport-Security" in schema.plain_text(t.content_json["description"])
        assert "HSTS" in schema.plain_text(t.content_json["remediation"])


def test_create_template_requires_name_and_valid_severity(client, stub_host):
    assert client.post(f"{M}/templates", json={}).status_code == 400
    assert client.post(f"{M}/templates", json={"name": "X", "default_severity": "spicy"}).status_code == 400


def test_create_template_sanitizes_a_malicious_content_block(client, stub_host, session_factory):
    """A template's prose is stored content too — the same write-time sanitizer must apply."""
    resp = client.post(
        f"{M}/templates",
        json={
            "name": "XSS template",
            "content_json": {
                "description": {
                    "type": "doc",
                    "content": [{"type": "html", "html": "<img src=x onerror=alert(1)>"}],
                }
            },
        },
    )
    assert resp.status_code == 201, resp.get_json()
    tid = resp.get_json()["id"]
    with session_factory() as db:
        desc = db.get(fm.VulnerabilityTemplate, tid).content_json["description"]
    assert "html" not in _node_types(desc)
    assert "onerror" not in str(desc)


# ── GET /engagements — scoped list ───────────────────────────────────────────────────────────────────


def test_list_engagements_is_scoped_to_the_tokens_clients(client, stub_host, session_factory):
    """A read token enumerates only engagements under clients it holds a grant on — never the whole table
    (the same tenancy the cookie dashboard uses via ``visible_engagements``)."""
    mine = _make_engagement(session_factory, name="mine", client_id=ACME)
    _make_engagement(session_factory, name="foreign", client_id=OTHER_CLIENT)

    # a NON-admin operator whose only grant is on ACME
    stub_host.actor = StubActor(id=7, username="op", role="operator")
    stub_host.viewable_client_ids = {ACME}

    body = client.get(f"{M}/engagements").get_json()
    ids = {e["id"] for e in body["items"]}
    assert ids == {mine}
    assert body["count"] == 1
    assert body["items"][0]["client_id"] == str(ACME)  # host id is stringified on the wire


def test_list_engagements_admin_sees_all(client, stub_host, session_factory):
    a = _make_engagement(session_factory, name="a", client_id=ACME)
    b = _make_engagement(session_factory, name="b", client_id=OTHER_CLIENT)
    # default fixture actor is admin
    ids = {e["id"] for e in client.get(f"{M}/engagements").get_json()["items"]}
    assert {a, b} <= ids


# ── GET /engagements/{id} — one engagement + counts, and the 404-sameness ────────────────────────────


def test_get_engagement_returns_counts(client, stub_host, session_factory):
    eid = _make_engagement(session_factory)
    _finding_with_body(session_factory, eid, title="F1")
    body = client.get(f"{M}/engagements/{eid}").get_json()
    assert body["id"] == eid
    assert body["finding_count"] == 1
    assert body["group_count"] == 0
    assert body["artifact_count"] == 0


def test_get_engagement_foreign_and_missing_are_the_same_404(client, stub_host, session_factory):
    """No existence oracle: an engagement under a client the token can't see is INDISTINGUISHABLE from
    one that does not exist — both 404, byte-identical."""
    foreign = _make_engagement(session_factory, client_id=OTHER_CLIENT)
    stub_host.actor = StubActor(id=9, username="stranger", role="operator")
    stub_host.viewable_client_ids = set()  # no grants at all

    r_foreign = client.get(f"{M}/engagements/{foreign}")
    r_missing = client.get(f"{M}/engagements/999999")
    assert r_foreign.status_code == r_missing.status_code == 404
    assert r_foreign.get_json() == r_missing.get_json()


def test_add_finding_to_foreign_engagement_is_404(client, stub_host, session_factory):
    """The destination-tenancy check on the write path: a foreign engagement 404s before the body even
    matters, so a caller can't diff a 400 (bad body) against a 404 (no access) to map the id space."""
    foreign = _make_engagement(session_factory, client_id=OTHER_CLIENT)
    stub_host.actor = StubActor(id=9, username="stranger", role="operator")
    stub_host.viewable_client_ids = set()
    resp = client.post(
        f"{M}/engagements/{foreign}/findings",
        json={"title": "sneak", "severity": "high"},
    )
    assert resp.status_code == 404


# ── GET /engagements/{id}/report — stream the deliverable ────────────────────────────────────────────


def test_report_html_is_non_empty(client, stub_host, session_factory):
    eid = _make_engagement(session_factory, name="Q3 Assessment")
    _finding_with_body(session_factory, eid, title="Weak SMB signing")
    resp = client.get(f"{M}/engagements/{eid}/report")  # default format=html
    assert resp.status_code == 200
    assert resp.mimetype == "text/html"
    assert len(resp.get_data()) > 0
    assert b"Weak SMB signing" in resp.get_data()


def test_report_docx_is_non_empty(client, stub_host, session_factory):
    eid = _make_engagement(session_factory, name="Q3 Assessment")
    _finding_with_body(session_factory, eid, title="Weak SMB signing")
    resp = client.get(f"{M}/engagements/{eid}/report?format=docx")
    assert resp.status_code == 200
    assert resp.mimetype == (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )
    body = resp.get_data()
    assert len(body) > 0
    assert body[:2] == b"PK"  # a .docx is a zip container


def test_report_rejects_pdf_and_unknown_formats(client, stub_host, session_factory):
    eid = _make_engagement(session_factory)
    assert client.get(f"{M}/engagements/{eid}/report?format=pdf").status_code == 400
    assert client.get(f"{M}/engagements/{eid}/report?format=bogus").status_code == 400


def test_report_of_foreign_engagement_is_404(client, stub_host, session_factory):
    foreign = _make_engagement(session_factory, client_id=OTHER_CLIENT)
    stub_host.actor = StubActor(id=9, username="stranger", role="operator")
    stub_host.viewable_client_ids = set()
    assert client.get(f"{M}/engagements/{foreign}/report").status_code == 404


# ── @host.require_scope enforcement (read vs write), against a REAL scope-checking gate ───────────────


def test_require_scope_read_token_cannot_write(app, client, stub_host):
    _install_scope_enforcing_gate(app, stub_host)
    stub_host.actor = StubActor(id=5, username="ro", role="operator", scopes=frozenset({"read"}))

    # a read route is reachable with a read token …
    assert client.get(f"{M}/templates").status_code == 200
    # … but every write route is refused (403), and nothing is written.
    assert client.post(f"{M}/templates", json={"name": "nope"}).status_code == 403
    assert client.post(f"{M}/engagements", json={"name": "nope", "client_id": ACME}).status_code == 403


def test_require_scope_write_token_can_write(app, client, stub_host):
    _install_scope_enforcing_gate(app, stub_host)
    stub_host.actor = StubActor(id=6, username="rw", role="operator", scopes=frozenset({"read", "write"}))
    assert client.post(f"{M}/templates", json={"name": "ok"}).status_code == 201
