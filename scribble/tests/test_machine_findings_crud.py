"""ext#41 — findings CRUD + board management on the PAT/machine API (`scribble/api_pat.py`).

Before this surface existed the machine API could CREATE a finding and nothing else: 13 routes, one of
them findings-related. An agent authoring a real deliverable over a PAT (which is how this was reported)
could not read back what it had created, could not fix a title, and could not group or order anything —
and its only recovery, delete-and-recreate, was also unavailable and would have discarded group
membership and position.

What this module proves, in three groups:

1. **It works** — list/read/edit/delete/move, group create/rename/delete/reorder, and that the board
   ordering the machine surface produces is the SAME ordering the cookie board and the report use
   (`findings_service`, shared by both surfaces).
2. **It cannot cross tenants** — a finding or group belonging to another engagement is not addressable,
   missing and not-visible are byte-identical 404s, and the bulk move is atomic so a foreign id in the
   list cannot be used as a probe or leave the board half-arranged.
3. **The seams are honoured** — every new write route declares `write` scope (proven against a REAL
   scope-checking gate, not the conftest's no-op), routes through the host `Idempotency-Key` seam, and
   emits an `ext:scribble:*` audit row.

The tenancy SWEEP over every machine route (each new route × each method, denied for a foreign token and
allowed for a granted one) lives in `tests/test_scribble_machine_tenancy.py`, which is where the
route-classification guard is; this module holds the per-route behaviour.
"""

from __future__ import annotations

import base64
import functools

import pytest
from flask import jsonify

import scribble.models as fm
from scribble.artifacts_storage import resolve_path
from scribble.content import schema
from scribble.enums import OrderMode, Severity
from tests.conftest import StubActor

M = "/scribble/machine"

ACME = 701          # the client the token under test holds
OTHER_CLIENT = 702  # a client it does not

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


# ── fixtures / helpers ───────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def token(stub_host):
    """A write-scoped, NON-admin token holding exactly one client grant.

    Non-admin deliberately: the conftest default actor is an admin, and the stub host's `can_view_client`
    lets an admin see every client — so an admin fixture would make every tenancy assertion below vacuous.
    """
    stub_host.actor = StubActor(id=21, username="report-bot", role="operator")
    stub_host.viewable_client_ids = {ACME}
    return stub_host


def _engagement(session_factory, *, client_id=ACME, name="Q3 external") -> int:
    with session_factory() as db:
        eng = fm.Engagement(name=name, scope_type="external", client_id=client_id)
        db.add(eng)
        db.commit()
        return eng.id


def _group(session_factory, engagement_id: int, name: str, *, order_index: int = 0) -> int:
    with session_factory() as db:
        group = fm.FindingGroup(engagement_id=engagement_id, name=name, order_index=order_index)
        db.add(group)
        db.commit()
        return group.id


def _finding(
    session_factory,
    engagement_id: int,
    *,
    title: str = "SMB signing not required",
    severity: Severity = Severity.medium,
    group_id: int | None = None,
    order_index: int = 0,
    **kw,
) -> int:
    with session_factory() as db:
        finding = fm.EngagementFinding(
            engagement_id=engagement_id,
            group_id=group_id,
            title=title,
            severity=severity,
            order_index=order_index,
            content_json={"description": schema.doc_from_text("original prose")},
            **kw,
        )
        db.add(finding)
        db.commit()
        return finding.id


def _install_scope_enforcing_gate(app, stub_host) -> None:
    """Replace the conftest's NO-OP `require_pat_scope` with one that REALLY checks the actor's scopes.

    Scope RBAC is the host's concern, but WHICH scope each route declares is scribble's — and that is only
    provable if a read token is actually refused by a write route. Under the no-op stub every route looks
    correctly gated even with its decorator missing or naming the wrong scope. (Same helper as
    `tests/test_machine_authoring.py`; duplicated rather than shared because these two modules test
    different route sets and a shared fixture would couple them.)
    """
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


def _install_audit_recorder(app) -> list[tuple]:
    """Wire a fake host `audit` hook (the seam `api_pat._audit` reaches through) and return its log."""
    recorded: list[tuple] = []

    def audit(_db, event, *, subject_type=None, subject_id=None, before=None, after=None):
        recorded.append((event, subject_type, subject_id, before, after))

    app.extensions["scribble"].extras["audit"] = audit
    return recorded


def _install_idempotency_seam(app) -> dict:
    """Wire a fake host `idempotent` hook: replay the stored response for a (actor, key) already seen,
    instead of executing `produce` a second time.

    Deliberately a REPLAY-ONLY fake. The real seam also 422s a different request under the same key, which
    is the host's contract and is tested there; what these tests need to know is only whether the routes
    route their mutation THROUGH the seam at all — a route that ignored it would execute twice, which is
    exactly what a retrying agent must never cause.
    """
    store: dict[tuple, tuple] = {}
    calls: dict[str, int] = {"produced": 0}

    def idempotent(actor, key, produce):
        actor_id = getattr(actor, "id", None)
        if (actor_id, key) in store:
            return store[(actor_id, key)]
        calls["produced"] += 1
        result = produce()
        store[(actor_id, key)] = result
        return result

    app.extensions["scribble"].extras["idempotent"] = idempotent
    return calls


# ── 1. read back: list + detail ──────────────────────────────────────────────────────────────────────


def test_list_findings_returns_groups_ungrouped_and_board_order(client, token, session_factory):
    """The gap the client actually hit: `GET /engagements/<id>` answers a bare `finding_count`, so an
    agent could not read back what it created. This lists every finding, grouped, IN BOARD ORDER.

    The order assertion is the load-bearing one — an `auto_severity` group renders worst-first, which is
    the order the REPORT will use, so a listing sorted by id (or by insertion) would mislead an agent
    about the document it is authoring.
    """
    eid = _engagement(session_factory)
    gid = _group(session_factory, eid, "Web Application")
    low = _finding(session_factory, eid, title="Cookie flags", severity=Severity.low, group_id=gid,
                   order_index=0)
    crit = _finding(session_factory, eid, title="SQL injection", severity=Severity.critical,
                    group_id=gid, order_index=1)
    loose = _finding(session_factory, eid, title="Banner disclosure", severity=Severity.info)

    resp = client.get(f"{M}/engagements/{eid}/findings")
    assert resp.status_code == 200, resp.get_json()
    body = resp.get_json()

    assert body["engagement_id"] == eid
    assert body["count"] == 3
    assert [g["id"] for g in body["groups"]] == [gid]
    section = body["groups"][0]
    assert section["name"] == "Web Application"
    assert section["order_mode"] == "auto_severity"
    # worst-first inside an auto_severity group, NOT order_index order
    assert [f["id"] for f in section["findings"]] == [crit, low]
    assert [f["severity"] for f in section["findings"]] == ["critical", "low"]
    assert [f["id"] for f in body["ungrouped"]] == [loose]


def test_get_finding_returns_content_evidence_and_children(client, token, session_factory, app):
    """One finding, in full — prose blocks, evidence artifacts, and the promoted per-host CHILDREN.

    Children matter: nesting is produced by promotion, a child carries its own target and evidence, and
    there is no ORM relationship to walk (only `parent_id`). An agent that could not see them would keep
    re-authoring rows that already exist.
    """
    eid = _engagement(session_factory)
    parent = _finding(session_factory, eid, title="Weak TLS configuration")
    child = _finding(session_factory, eid, title="Weak TLS on host-2", parent_id=parent,
                     target_host="10.0.0.2")
    upload = client.post(
        f"{M}/engagements/{eid}/artifacts",
        json={"filename": "tls.png", "content_base64": base64.b64encode(PNG).decode(),
              "finding_id": parent, "caption": "scanner output"},
    )
    assert upload.status_code == 201, upload.get_json()

    resp = client.get(f"{M}/findings/{parent}")
    assert resp.status_code == 200, resp.get_json()
    body = resp.get_json()

    assert body["id"] == parent
    assert body["engagement_id"] == eid
    assert body["title"] == "Weak TLS configuration"
    assert "description" in body["content_json"]
    assert [a["caption"] for a in body["artifacts"]] == ["scanner output"]
    assert [c["id"] for c in body["children"]] == [child]
    assert body["children"][0]["target_host"] == "10.0.0.2"


# ── 2. edit in place (PATCH) ─────────────────────────────────────────────────────────────────────────


def test_patch_updates_only_the_fields_supplied(client, token, session_factory):
    """Partial by definition: an omitted field must NOT be cleared. The original bug report was "fixing
    wording means delete and recreate"; a PATCH that wiped everything it wasn't told about would be a
    worse version of the same problem."""
    eid = _engagement(session_factory)
    fid = _finding(session_factory, eid, title="Old title", severity=Severity.medium,
                   category="Network", target_host="10.0.0.1")

    resp = client.patch(
        f"{M}/findings/{fid}",
        json={"title": "  Weak SMB signing  ", "severity": "high", "cvss_score": 7.5,
              "cvss_vector": "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:N/A:N", "target_port": 445},
    )
    assert resp.status_code == 200, resp.get_json()
    body = resp.get_json()
    assert body["title"] == "Weak SMB signing"  # trimmed
    assert body["severity"] == "high"
    assert body["cvss_score"] == 7.5
    assert body["target_port"] == "445"  # coerced to the column's string type

    with session_factory() as db:
        row = db.get(fm.EngagementFinding, fid)
        assert row.title == "Weak SMB signing"
        assert row.category == "Network"      # untouched
        assert row.target_host == "10.0.0.1"  # untouched


def test_patch_null_clears_a_nullable_field(client, token, session_factory):
    """An explicit null is a CLEAR, and is distinguishable from an omitted field (the `_ABSENT` sentinel
    in `_parse_finding_patch` is what makes those two different)."""
    eid = _engagement(session_factory)
    fid = _finding(session_factory, eid, category="Network", cvss_score=4.0)

    resp = client.patch(f"{M}/findings/{fid}", json={"category": None, "cvss_score": None})
    assert resp.status_code == 200, resp.get_json()
    with session_factory() as db:
        row = db.get(fm.EngagementFinding, fid)
        assert row.category is None
        assert row.cvss_score is None


def test_patch_merges_content_blocks_and_sanitizes_them(client, token, session_factory):
    """Prose edits land per BLOCK (an untouched block survives), and everything a write-scoped token
    supplies is sanitized before persist — the stored-XSS gate. A PATCH must not be a second, laxer way
    into `content_json` than the create route.
    """
    eid = _engagement(session_factory)
    fid = _finding(session_factory, eid)
    with session_factory() as db:
        row = db.get(fm.EngagementFinding, fid)
        row.content_json = {"description": schema.doc_from_text("keep me"),
                            "remediation": schema.doc_from_text("old remediation")}
        db.commit()

    hostile = {
        "type": "doc",
        "content": [{"type": "paragraph", "content": [
            {"type": "text", "text": "click",
             "marks": [{"type": "link", "attrs": {"href": "javascript:alert(1)"}}]},
        ]}],
    }
    resp = client.patch(
        f"{M}/findings/{fid}",
        json={"remediation": "Enable SMB signing on every domain controller.",
              "content_json": {"impact": hostile}},
    )
    assert resp.status_code == 200, resp.get_json()

    with session_factory() as db:
        content = db.get(fm.EngagementFinding, fid).content_json
        html = db.get(fm.EngagementFinding, fid).content_html
    assert "keep me" in str(content["description"])              # untouched block survived
    assert "Enable SMB signing" in str(content["remediation"])   # plain text was wrapped into a doc
    assert "javascript:" not in str(content["impact"])           # sanitized on the way in
    # content_html is re-derived for the blocks that changed, so the editor/preview cache cannot drift.
    assert "Enable SMB signing" in html["remediation"]


def test_patch_rejects_an_unknown_field(client, token, session_factory):
    """A typo'd field name must be a refusal, not a 200 for an edit that never happened. This module's
    house style is lenient parsing; the edit route deliberately breaks it, because "silent success that
    did nothing" is the worse failure for an agent that cannot see the result."""
    eid = _engagement(session_factory)
    fid = _finding(session_factory, eid, title="Original")

    resp = client.patch(f"{M}/findings/{fid}", json={"titel": "typo", "sevrity": "high"})
    assert resp.status_code == 400
    detail = resp.get_json()["detail"]
    assert "sevrity" in detail and "titel" in detail
    with session_factory() as db:
        assert db.get(fm.EngagementFinding, fid).title == "Original"


def test_patch_points_group_id_and_order_index_at_the_move_route(client, token, session_factory):
    """`group_id`/`order_index` are the two fields a caller most plausibly expects PATCH to take. They
    belong to `move`, which owns the ordering semantics — so the refusal names it instead of being a bare
    "unknown field"."""
    eid = _engagement(session_factory)
    gid = _group(session_factory, eid, "Web")
    fid = _finding(session_factory, eid)

    for body in ({"group_id": gid}, {"order_index": 2}):
        resp = client.patch(f"{M}/findings/{fid}", json=body)
        assert resp.status_code == 400, resp.get_json()
        assert "/move" in resp.get_json()["detail"]
    with session_factory() as db:
        assert db.get(fm.EngagementFinding, fid).group_id is None


@pytest.mark.parametrize(
    "body",
    [
        {},                              # nothing to do — say so rather than 200 for a no-op
        {"title": "   "},                # a blank title would silently keep the old one on the cookie form
        {"severity": "catastrophic"},
        {"confidence": 5},
        {"status": "wontfix"},
        {"cvss_score": "high"},
        {"include_in_report": "yes"},
        {"category": {"nested": 1}},
        {"content_json": "not-an-object"},
        {"references": "not-a-list"},
    ],
)
def test_patch_rejects_malformed_input(client, token, session_factory, body):
    eid = _engagement(session_factory)
    fid = _finding(session_factory, eid, title="Original")
    resp = client.patch(f"{M}/findings/{fid}", json=body)
    assert resp.status_code == 400, (body, resp.get_json())
    assert resp.get_json()["error"] == "bad_request"
    with session_factory() as db:
        assert db.get(fm.EngagementFinding, fid).title == "Original"


# ── 3. delete ────────────────────────────────────────────────────────────────────────────────────────


def test_delete_finding_takes_its_evidence_rows_and_files(client, token, session_factory, app):
    """A finding IS its content, so deleting it takes its artifacts — DB rows AND the bytes on disk.

    Without the explicit cascade `findings_service.delete_finding` performs, a bare ORM delete would NULL
    each artifact's nullable `finding_id` instead: the row survives, orphaned, and the file leaks.
    """
    cfg = app.extensions["scribble"]
    eid = _engagement(session_factory)
    fid = _finding(session_factory, eid)
    upload = client.post(
        f"{M}/engagements/{eid}/artifacts",
        json={"filename": "evidence.png", "content_base64": base64.b64encode(PNG).decode(),
              "finding_id": fid},
    )
    assert upload.status_code == 201, upload.get_json()
    artifact_id = upload.get_json()["id"]
    with session_factory() as db:
        on_disk = resolve_path(cfg, db.get(fm.Artifact, artifact_id).storage_path)
    assert on_disk.is_file()

    resp = client.delete(f"{M}/findings/{fid}")
    assert resp.status_code == 200, resp.get_json()
    assert resp.get_json() == {"deleted": True, "finding_id": fid, "engagement_id": eid}

    with session_factory() as db:
        assert db.get(fm.EngagementFinding, fid) is None
        assert db.get(fm.Artifact, artifact_id) is None
    assert not on_disk.is_file()


def test_delete_finding_twice_is_a_404_the_second_time(client, token, session_factory):
    """Without an idempotency key there is nothing to replay, so a second delete is an honest 404 — the
    same one a nonexistent id gets."""
    eid = _engagement(session_factory)
    fid = _finding(session_factory, eid)
    assert client.delete(f"{M}/findings/{fid}").status_code == 200
    second = client.delete(f"{M}/findings/{fid}")
    assert second.status_code == 404
    assert second.get_json() == {"error": "not_found", "detail": "finding not found"}


# ── 4. move: single ──────────────────────────────────────────────────────────────────────────────────


def test_move_finding_into_a_group_and_flip_it_to_manual(client, token, session_factory):
    """The client's core complaint: an agent could not group or order anything. A move sets both, and any
    move flips the destination group to manual ordering (a deliberate placement beats severity ranking)."""
    eid = _engagement(session_factory)
    gid = _group(session_factory, eid, "Internal")
    existing = _finding(session_factory, eid, title="First", group_id=gid, order_index=0)
    fid = _finding(session_factory, eid, title="Loose", severity=Severity.info)

    resp = client.post(f"{M}/findings/{fid}/move", json={"group_id": gid, "order_index": 0})
    assert resp.status_code == 200, resp.get_json()
    body = resp.get_json()
    assert (body["group_id"], body["order_index"]) == (gid, 0)

    with session_factory() as db:
        group = db.get(fm.FindingGroup, gid)
        assert group.order_mode == OrderMode.manual
        assert [f.id for f in sorted(group.findings, key=lambda f: f.order_index)] == [fid, existing]


def test_move_finding_to_ungrouped(client, token, session_factory):
    """`group_id: null` is the ungrouped bucket — and it must be an explicit null, not an omission."""
    eid = _engagement(session_factory)
    gid = _group(session_factory, eid, "Internal")
    fid = _finding(session_factory, eid, group_id=gid)

    resp = client.post(f"{M}/findings/{fid}/move", json={"group_id": None})
    assert resp.status_code == 200, resp.get_json()
    assert resp.get_json()["group_id"] is None
    with session_factory() as db:
        assert db.get(fm.EngagementFinding, fid).group_id is None


def test_move_requires_the_group_id_key(client, token, session_factory):
    """An absent `group_id` is a 400, not "leave it where it is": a move with no stated destination is a
    guess about what the caller wanted (the cookie route's rule, kept)."""
    eid = _engagement(session_factory)
    fid = _finding(session_factory, eid)
    resp = client.post(f"{M}/findings/{fid}/move", json={"order_index": 1})
    assert resp.status_code == 400
    assert "group_id is required" in resp.get_json()["detail"]


def test_move_into_a_foreign_engagements_group_is_404_and_moves_nothing(client, token, session_factory):
    """A group id from ANOTHER engagement must not be attachable.

    Note the deliberate divergence from `add_finding`, which silently DROPS a foreign `group_id`: on a
    move the destination group IS the request, so dropping it would move the finding out of whatever group
    it was in — data loss the caller never asked for, reported as success. This mirrors the cookie move
    route (`engagement_ui.move_finding`), which 404s for exactly this case.
    """
    mine = _engagement(session_factory)
    theirs = _engagement(session_factory, name="Theirs")  # same client: this is NOT a tenancy test
    home = _group(session_factory, mine, "Mine")
    foreign_group = _group(session_factory, theirs, "Theirs")
    fid = _finding(session_factory, mine, group_id=home)

    resp = client.post(f"{M}/findings/{fid}/move", json={"group_id": foreign_group})
    assert resp.status_code == 404
    with session_factory() as db:
        assert db.get(fm.EngagementFinding, fid).group_id == home  # still where it was


def test_the_group_refusal_is_identical_for_foreign_and_nonexistent(client, token, session_factory):
    """One message for both, so the refusal confirms nothing about which group ids exist."""
    mine = _engagement(session_factory)
    theirs = _engagement(session_factory, name="Theirs")
    foreign_group = _group(session_factory, theirs, "Theirs")
    fid = _finding(session_factory, mine)

    foreign = client.post(f"{M}/findings/{fid}/move", json={"group_id": foreign_group})
    missing = client.post(f"{M}/findings/{fid}/move", json={"group_id": 987654})
    assert (foreign.status_code, foreign.data) == (missing.status_code, missing.data)


# ── 5. move: bulk (the multi-select drag) ────────────────────────────────────────────────────────────


def test_bulk_move_preserves_the_listed_order(client, token, session_factory):
    """"Multi-select and drag several findings into a group at once" — one call, listed order preserved.

    One-at-a-time works but is N round trips, and a failure part-way leaves the board half-arranged.
    """
    eid = _engagement(session_factory)
    gid = _group(session_factory, eid, "Web Application")
    a = _finding(session_factory, eid, title="A", severity=Severity.low)
    b = _finding(session_factory, eid, title="B", severity=Severity.critical)
    c = _finding(session_factory, eid, title="C", severity=Severity.medium)

    resp = client.post(
        f"{M}/engagements/{eid}/findings/move",
        json={"finding_ids": [b, c, a], "group_id": gid, "order_index": 0},
    )
    assert resp.status_code == 200, resp.get_json()
    assert [m["finding_id"] for m in resp.get_json()["moved"]] == [b, c, a]

    with session_factory() as db:
        group = db.get(fm.FindingGroup, gid)
        ordered = sorted(group.findings, key=lambda f: f.order_index)
        assert [f.id for f in ordered] == [b, c, a]
        assert [f.order_index for f in ordered] == [0, 1, 2]  # no gaps, no duplicates


def test_bulk_move_with_a_foreign_finding_id_moves_nothing(client, token, session_factory):
    """ATOMIC. One id outside this engagement refuses the whole request with the same `finding not found`
    a nonexistent id gets, and NOTHING moves.

    A partial success that skipped unknown ids would be strictly worse in two ways: the caller cannot tell
    a complete move from a partial one, and skipping (rather than refusing) is what would turn a foreign id
    into a probe.
    """
    eid = _engagement(session_factory)
    other = _engagement(session_factory, name="Other")
    gid = _group(session_factory, eid, "Web")
    mine = _finding(session_factory, eid, title="Mine")
    theirs = _finding(session_factory, other, title="Theirs")

    resp = client.post(
        f"{M}/engagements/{eid}/findings/move",
        json={"finding_ids": [mine, theirs], "group_id": gid},
    )
    assert resp.status_code == 404
    assert resp.get_json() == {"error": "not_found", "detail": "finding not found"}
    with session_factory() as db:
        assert db.get(fm.EngagementFinding, mine).group_id is None  # the legal one did NOT move
        assert db.get(fm.EngagementFinding, theirs).group_id is None


def test_bulk_move_collapses_duplicate_ids(client, token, session_factory):
    """A repeated id is a multi-select artefact, not an error — it must not place the same finding twice
    and leave a hole in the ordering."""
    eid = _engagement(session_factory)
    gid = _group(session_factory, eid, "Web")
    a = _finding(session_factory, eid, title="A")
    b = _finding(session_factory, eid, title="B")

    resp = client.post(
        f"{M}/engagements/{eid}/findings/move",
        json={"finding_ids": [a, a, b], "group_id": gid},
    )
    assert resp.status_code == 200, resp.get_json()
    assert [m["finding_id"] for m in resp.get_json()["moved"]] == [a, b]
    with session_factory() as db:
        ordered = sorted(db.get(fm.FindingGroup, gid).findings, key=lambda f: f.order_index)
        assert [f.order_index for f in ordered] == [0, 1]


@pytest.mark.parametrize(
    "body",
    [
        {"group_id": None},                          # no finding_ids
        {"finding_ids": [], "group_id": None},       # empty list
        {"finding_ids": ["abc"], "group_id": None},  # not an id
        {"finding_ids": [True], "group_id": None},   # bool is an int subclass — not an id
    ],
)
def test_bulk_move_rejects_malformed_input(client, token, session_factory, body):
    eid = _engagement(session_factory)
    resp = client.post(f"{M}/engagements/{eid}/findings/move", json=body)
    assert resp.status_code == 400, (body, resp.get_json())


# ── 6. groups ────────────────────────────────────────────────────────────────────────────────────────


def test_create_rename_and_reorder_groups(client, token, session_factory):
    """Sections are what turn a pile of findings into a report, and the machine API had no way to make
    one. Create appends last; PATCH renames and re-ranks; reorder sets the top-to-bottom order the report
    renders."""
    eid = _engagement(session_factory)

    first = client.post(f"{M}/engagements/{eid}/groups", json={"name": "External"})
    second = client.post(f"{M}/engagements/{eid}/groups", json={"name": "Intrnal"})
    assert (first.status_code, second.status_code) == (201, 201)
    gid1, gid2 = first.get_json()["id"], second.get_json()["id"]
    assert [first.get_json()["order_index"], second.get_json()["order_index"]] == [0, 1]

    renamed = client.patch(
        f"{M}/engagements/{eid}/groups/{gid2}",
        json={"name": "Internal", "include_in_report": False, "order_mode": "manual"},
    )
    assert renamed.status_code == 200, renamed.get_json()
    assert renamed.get_json()["name"] == "Internal"
    assert renamed.get_json()["include_in_report"] is False
    assert renamed.get_json()["order_mode"] == "manual"

    reordered = client.post(f"{M}/engagements/{eid}/groups/reorder", json={"order": [gid2, gid1]})
    assert reordered.status_code == 200, reordered.get_json()
    assert reordered.get_json()["order"] == [
        {"id": gid2, "order_index": 0}, {"id": gid1, "order_index": 1}
    ]
    with session_factory() as db:
        assert db.get(fm.FindingGroup, gid2).order_index == 0
        assert db.get(fm.FindingGroup, gid1).order_index == 1


def test_rerank_by_severity_is_the_way_back_from_manual(client, token, session_factory):
    """A move flips a group to manual; `order_mode: auto_severity` is the documented way back. Without it
    an agent that dragged once could never restore severity ranking."""
    eid = _engagement(session_factory)
    gid = _group(session_factory, eid, "Web")
    fid = _finding(session_factory, eid)
    client.post(f"{M}/findings/{fid}/move", json={"group_id": gid})
    with session_factory() as db:
        assert db.get(fm.FindingGroup, gid).order_mode == OrderMode.manual

    resp = client.patch(f"{M}/engagements/{eid}/groups/{gid}", json={"order_mode": "auto_severity"})
    assert resp.status_code == 200, resp.get_json()
    with session_factory() as db:
        assert db.get(fm.FindingGroup, gid).order_mode == OrderMode.auto_severity


def test_delete_group_detaches_its_findings_instead_of_deleting_them(client, token, session_factory):
    """Deleting a report SECTION must never silently destroy authored findings — they go back to the
    ungrouped bucket. (Contrast the finding delete above, which does take its evidence with it.)"""
    eid = _engagement(session_factory)
    gid = _group(session_factory, eid, "Web")
    fid = _finding(session_factory, eid, group_id=gid)

    resp = client.delete(f"{M}/engagements/{eid}/groups/{gid}")
    assert resp.status_code == 200, resp.get_json()
    assert resp.get_json()["detached_finding_ids"] == [fid]
    with session_factory() as db:
        assert db.get(fm.FindingGroup, gid) is None
        survivor = db.get(fm.EngagementFinding, fid)
        assert survivor is not None and survivor.group_id is None


def test_group_routes_refuse_another_engagements_group(client, token, session_factory):
    """The URL names an engagement AND a group; a group belonging to a different engagement is a 404 on
    both the edit and the delete, even when the caller may see both engagements."""
    mine = _engagement(session_factory)
    theirs = _engagement(session_factory, name="Theirs")
    foreign_group = _group(session_factory, theirs, "Theirs")

    assert client.patch(
        f"{M}/engagements/{mine}/groups/{foreign_group}", json={"name": "hijacked"}
    ).status_code == 404
    assert client.delete(f"{M}/engagements/{mine}/groups/{foreign_group}").status_code == 404
    with session_factory() as db:
        assert db.get(fm.FindingGroup, foreign_group).name == "Theirs"


@pytest.mark.parametrize(
    "path,body",
    [
        ("groups", {}),                                  # name required
        ("groups", {"name": "   "}),
        ("groups/reorder", {}),                          # order required
        ("groups/reorder", {"order": "1,2"}),
    ],
)
def test_group_routes_reject_malformed_input(client, token, session_factory, path, body):
    eid = _engagement(session_factory)
    resp = client.post(f"{M}/engagements/{eid}/{path}", json=body)
    assert resp.status_code == 400, (path, body, resp.get_json())


@pytest.mark.parametrize(
    "body", [{}, {"name": ""}, {"order_mode": "by_vibes"}, {"include_in_report": "no"}, {"colour": "red"}]
)
def test_group_patch_rejects_malformed_input(client, token, session_factory, body):
    eid = _engagement(session_factory)
    gid = _group(session_factory, eid, "Web")
    resp = client.patch(f"{M}/engagements/{eid}/groups/{gid}", json=body)
    assert resp.status_code == 400, (body, resp.get_json())
    with session_factory() as db:
        assert db.get(fm.FindingGroup, gid).name == "Web"


# ── 7. tenancy: a finding in another tenant's engagement is not addressable ──────────────────────────


_FINDING_ROUTE_CALLS = [
    ("GET", "{M}/findings/{fid}", None),
    ("PATCH", "{M}/findings/{fid}", {"title": "hijacked"}),
    ("DELETE", "{M}/findings/{fid}", None),
    ("POST", "{M}/findings/{fid}/move", {"group_id": None}),
]


@pytest.mark.parametrize("method,template,body", _FINDING_ROUTE_CALLS)
def test_every_findings_route_denies_a_foreign_finding(client, token, session_factory, method,
                                                       template, body):
    """The threat model is not an unauthenticated stranger: it is a legitimate write-scoped token of
    tenant B addressing tenant A's finding id directly.

    `/findings/<id>` carries no engagement in the URL, so tenancy is resolved from the ROW's own
    `engagement_id` (`api_pat._visible_finding`) — never from anything the caller supplied. Every route
    must answer the same 404, and nothing may change.
    """
    foreign_engagement = _engagement(session_factory, client_id=OTHER_CLIENT, name="Theirs")
    fid = _finding(session_factory, foreign_engagement, title="Their finding")

    url = template.format(M=M, fid=fid)
    resp = client.open(url, method=method, json=body)
    assert resp.status_code == 404, resp.get_json()
    assert resp.get_json() == {"error": "not_found", "detail": "finding not found"}
    with session_factory() as db:
        survivor = db.get(fm.EngagementFinding, fid)
        assert survivor is not None and survivor.title == "Their finding"


@pytest.mark.parametrize("method,template,body", _FINDING_ROUTE_CALLS)
def test_a_foreign_finding_and_a_missing_one_are_byte_identical(client, token, session_factory, method,
                                                                template, body):
    """No existence oracle over the finding id space: "not yours" and "never existed" must be the same
    answer, byte for byte, or the ids are enumerable."""
    foreign_engagement = _engagement(session_factory, client_id=OTHER_CLIENT, name="Theirs")
    fid = _finding(session_factory, foreign_engagement)

    foreign = client.open(template.format(M=M, fid=fid), method=method, json=body)
    missing = client.open(template.format(M=M, fid=987654), method=method, json=body)
    assert (foreign.status_code, foreign.data) == (missing.status_code, missing.data)


def test_a_granted_token_can_reach_its_own_finding(client, token, session_factory):
    """The positive control for the two sweeps above. Without it, a denial test would also pass if the
    routes 404'd for everyone — the classic way an authorization test decays into a test of nothing."""
    eid = _engagement(session_factory)
    fid = _finding(session_factory, eid)
    assert client.get(f"{M}/findings/{fid}").status_code == 200
    assert client.patch(f"{M}/findings/{fid}", json={"title": "Mine, edited"}).status_code == 200


def test_bulk_move_cannot_be_pointed_at_a_foreign_engagement(client, token, session_factory):
    """The bulk route takes its engagement from the URL, so it is gated like every other engagement-scoped
    route — a foreign engagement is a 404 before the body is even read."""
    theirs = _engagement(session_factory, client_id=OTHER_CLIENT, name="Theirs")
    fid = _finding(session_factory, theirs)
    resp = client.post(
        f"{M}/engagements/{theirs}/findings/move", json={"finding_ids": [fid], "group_id": None}
    )
    assert resp.status_code == 404
    assert resp.get_json() == {"error": "not_found", "detail": "engagement not found"}


# ── 8. scope declarations, the idempotency seam, and audit rows ──────────────────────────────────────


_WRITE_ROUTE_CALLS = [
    ("PATCH", "{M}/findings/{fid}", {"title": "x"}),
    ("DELETE", "{M}/findings/{fid}", None),
    ("POST", "{M}/findings/{fid}/move", {"group_id": None}),
    ("POST", "{M}/engagements/{eid}/findings/move", {"finding_ids": [], "group_id": None}),
    ("POST", "{M}/engagements/{eid}/groups", {"name": "New"}),
    ("PATCH", "{M}/engagements/{eid}/groups/{gid}", {"name": "New"}),
    ("DELETE", "{M}/engagements/{eid}/groups/{gid}", None),
    ("POST", "{M}/engagements/{eid}/groups/reorder", {"order": []}),
]


def test_read_token_cannot_reach_any_new_write_route(app, client, stub_host, session_factory):
    """Every new mutating route must declare `write` scope. Proven against a REAL scope-checking gate:
    under the conftest's no-op `require_pat_scope` a route with a missing or wrongly-named decorator looks
    perfectly gated."""
    _install_scope_enforcing_gate(app, stub_host)
    stub_host.actor = StubActor(id=22, username="ro", role="operator", scopes=frozenset({"read"}))
    stub_host.viewable_client_ids = {ACME}
    eid = _engagement(session_factory)
    gid = _group(session_factory, eid, "Web")
    fid = _finding(session_factory, eid)

    reached = []
    for method, template, body in _WRITE_ROUTE_CALLS:
        url = template.format(M=M, eid=eid, gid=gid, fid=fid)
        resp = client.open(url, method=method, json=body)
        if resp.status_code != 403:
            reached.append((method, url, resp.status_code))
    assert reached == [], f"a read-only token reached write route(s): {reached}"

    # …and the read routes ARE reachable with it, so the sweep above is not just "everything 403s".
    assert client.get(f"{M}/findings/{fid}").status_code == 200
    assert client.get(f"{M}/engagements/{eid}/findings").status_code == 200


def test_a_write_token_reaches_the_same_routes(app, client, stub_host, session_factory):
    """Positive control for the scope sweep: the same URLs, with a write token, must not be 403."""
    _install_scope_enforcing_gate(app, stub_host)
    stub_host.actor = StubActor(id=23, username="rw", role="operator",
                               scopes=frozenset({"read", "write"}))
    stub_host.viewable_client_ids = {ACME}
    eid = _engagement(session_factory)

    forbidden = []
    for method, template, body in _WRITE_ROUTE_CALLS:
        gid = _group(session_factory, eid, "Web")
        fid = _finding(session_factory, eid)
        url = template.format(M=M, eid=eid, gid=gid, fid=fid)
        resp = client.open(url, method=method, json=body)
        if resp.status_code == 403:
            forbidden.append((method, url))
    assert forbidden == [], f"a write token was refused on: {forbidden}"


def test_a_retried_patch_with_the_same_key_executes_once(app, client, token, session_factory):
    """The mutating routes go through the host `Idempotency-Key` seam, so a tool that retries a
    timed-out request does not execute it twice.

    Asserted as "produce ran once", not merely "the two responses match": a route that ignored the seam
    would ALSO return matching responses for a repeated PATCH (it is naturally idempotent), so response
    equality alone would prove nothing about whether the seam is wired.
    """
    calls = _install_idempotency_seam(app)
    eid = _engagement(session_factory)
    fid = _finding(session_factory, eid)
    headers = {"Idempotency-Key": "patch-finding-1"}
    body = {"title": "Weak SMB signing"}

    first = client.patch(f"{M}/findings/{fid}", json=body, headers=headers)
    second = client.patch(f"{M}/findings/{fid}", json=body, headers=headers)
    assert first.status_code == 200, first.get_json()
    assert (second.status_code, second.get_json()) == (200, first.get_json())
    assert calls["produced"] == 1, "the second PATCH re-executed instead of replaying"


def test_a_retried_delete_404s_because_authorization_runs_BEFORE_the_seam(app, client, token,
                                                                         session_factory):
    """A retried DELETE answers 404, and that is the deliberate consequence of an ordering this module
    will not invert.

    `DELETE /findings/<id>` resolves and authorizes the finding FIRST (rule 2 of the section banner) and
    only then enters the idempotency seam. After a successful delete the row is gone, so the retry never
    reaches the seam to replay the stored 200. Making it replay would mean either consulting the seam
    before deciding whether the caller may touch the row, or letting the tenancy check pass on a row it
    can no longer resolve — and neither is worth a friendlier status code.

    Nothing is lost by this: DELETE's EFFECT is idempotent (the row is gone either way), and the seam is
    still doing its job on the case that matters — two CONCURRENT retries, where its DB-level unique
    constraint is what stops the second from executing a second delete on the same row.
    """
    _install_idempotency_seam(app)
    eid = _engagement(session_factory)
    fid = _finding(session_factory, eid)
    headers = {"Idempotency-Key": "delete-finding-1"}

    first = client.delete(f"{M}/findings/{fid}", headers=headers)
    second = client.delete(f"{M}/findings/{fid}", headers=headers)
    assert first.status_code == 200, first.get_json()
    assert second.status_code == 404
    assert second.get_json() == {"error": "not_found", "detail": "finding not found"}


def test_a_retried_group_create_with_the_same_key_makes_one_group(app, client, token, session_factory):
    """The same property where a duplicate is silent rather than loud: a retried create would otherwise
    leave two identically-named sections in the report."""
    _install_idempotency_seam(app)
    eid = _engagement(session_factory)
    headers = {"Idempotency-Key": "group-1"}

    first = client.post(f"{M}/engagements/{eid}/groups", json={"name": "External"}, headers=headers)
    second = client.post(f"{M}/engagements/{eid}/groups", json={"name": "External"}, headers=headers)
    assert first.status_code == 201, first.get_json()
    assert (second.status_code, second.get_json()) == (201, first.get_json())
    with session_factory() as db:
        assert db.query(fm.FindingGroup).filter_by(engagement_id=eid).count() == 1


def test_every_mutating_route_emits_an_audit_row(app, client, token, session_factory):
    """Editing, deleting, moving and re-sectioning a client's report are exactly the acts an audit trail
    exists for, and the neighbouring machine routes already emit one. A route that mutated silently would
    leave the trail describing an engagement that no longer looks like that."""
    recorded = _install_audit_recorder(app)
    eid = _engagement(session_factory)
    fid = _finding(session_factory, eid)

    created = client.post(f"{M}/engagements/{eid}/groups", json={"name": "External"})
    gid = created.get_json()["id"]
    assert client.patch(f"{M}/engagements/{eid}/groups/{gid}", json={"name": "Ext"}).status_code == 200
    assert client.patch(f"{M}/findings/{fid}", json={"title": "Edited"}).status_code == 200
    assert client.post(f"{M}/findings/{fid}/move", json={"group_id": gid}).status_code == 200
    assert client.post(
        f"{M}/engagements/{eid}/findings/move", json={"finding_ids": [fid], "group_id": None}
    ).status_code == 200
    assert client.post(f"{M}/engagements/{eid}/groups/reorder", json={"order": [gid]}).status_code == 200
    assert client.delete(f"{M}/engagements/{eid}/groups/{gid}").status_code == 200
    assert client.delete(f"{M}/findings/{fid}").status_code == 200

    events = [event for event, *_ in recorded]
    assert events == [
        "ext:scribble:create_group",
        "ext:scribble:update_group",
        "ext:scribble:update_finding",
        "ext:scribble:move_finding",
        "ext:scribble:move_findings",
        "ext:scribble:reorder_groups",
        "ext:scribble:delete_group",
        "ext:scribble:delete_finding",
    ]
    # A delete's audit row must carry the BEFORE state — after the row is gone it is the only record of
    # what was deleted.
    delete_finding_row = next(row for row in recorded if row[0] == "ext:scribble:delete_finding")
    assert delete_finding_row[3]["title"] == "Edited"


# ── 9. the two surfaces cannot drift ────────────────────────────────────────────────────────────────


def test_machine_and_cookie_moves_produce_the_same_board(client, token, session_factory):
    """The whole reason `findings_service` exists: the machine route and the cookie route must land a
    finding in the same place, because both feed the SAME report ordering.

    Driven through both HTTP surfaces rather than by calling the service twice — that would prove only
    that one function is deterministic, not that both routes call it.
    """
    eid = _engagement(session_factory)
    gid = _group(session_factory, eid, "Web")
    a = _finding(session_factory, eid, title="A", severity=Severity.low, group_id=gid, order_index=0)
    b = _finding(session_factory, eid, title="B", severity=Severity.critical, group_id=gid,
                 order_index=1)
    machine_moved = _finding(session_factory, eid, title="M", severity=Severity.medium)
    cookie_moved = _finding(session_factory, eid, title="C", severity=Severity.medium)

    # Same request, one through each surface: insert at slot 1 of an auto_severity group, whose RENDERED
    # order is [B(critical), A(low)] — so slot 1 means "after B", not "after the row with order_index 0".
    assert client.post(f"{M}/findings/{machine_moved}/move",
                       json={"group_id": gid, "order_index": 1}).status_code == 200
    with session_factory() as db:  # reset to the pre-move state for the cookie run
        group = db.get(fm.FindingGroup, gid)
        group.order_mode = OrderMode.auto_severity
        db.get(fm.EngagementFinding, machine_moved).group_id = None
        for finding_id, index in ((a, 0), (b, 1)):
            db.get(fm.EngagementFinding, finding_id).order_index = index
        db.commit()
    assert client.post(f"/scribble/api/findings/{cookie_moved}/move",
                       json={"group_id": gid, "order_index": 1}).status_code == 200

    with session_factory() as db:
        ordered = sorted(db.get(fm.FindingGroup, gid).findings, key=lambda f: f.order_index)
        # B first (it was the worst-severity row the board showed at slot 0), then the moved finding.
        assert [f.title for f in ordered] == ["B", "C", "A"]
