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
import uuid

import pytest
from sqlalchemy import select

import scribble.models as fm
from scribble.content import schema
from scribble.enums import OrderMode, Severity
from scribble.testing import read_evidence
from tests.conftest import StubActor, install_scope_enforcing_gate

M = "/scribble/machine"

# lotek#335 -- the standalone `Client` model is UUID-keyed since the PK migration, so a host client id
# (even one this file never inserts a row for -- `resolve_client` just needs a well-formed id to query)
# is a UUID, not the small int this file used before.
ACME = uuid.uuid7()          # the client the token under test holds
OTHER_CLIENT = uuid.uuid7()  # a client it does not

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


def _engagement(session_factory, *, client_id=ACME, name="Q3 external") -> str:
    with session_factory() as db:
        eng = fm.Engagement(name=name, scope_type="external", client_id=client_id)
        db.add(eng)
        db.commit()
        # str(): lotek#335 -- ids are UUIDv7, and the JSON responses this file compares against
        # round-trip a UUID as its string form, not a `uuid.UUID` object.
        return str(eng.id)


def _group(session_factory, engagement_id: str, name: str, *, order_index: int = 0) -> str:
    with session_factory() as db:
        group = fm.FindingGroup(engagement_id=engagement_id, name=name, order_index=order_index)
        db.add(group)
        db.commit()
        return str(group.id)


def _finding(
    session_factory,
    engagement_id: str,
    *,
    title: str = "SMB signing not required",
    severity: Severity = Severity.medium,
    group_id: str | None = None,
    order_index: int = 0,
    **kw,
) -> str:
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
        return str(finding.id)


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


def test_list_findings_is_the_board_list_and_says_what_the_report_renders(
    client, token, session_factory
):
    """A promoted parent's children are BOARD rows here, and the listing must not let a caller mistake
    `count` for "findings in the deliverable".

    The listing is flat on purpose — that is what the drag board shows, and `order_index` on a move is a
    slot in exactly this list. But the renderer nests each per-host child inside its parent's card, so a
    1-parent/2-child promotion is 3 rows here and ONE finding in the client's report. An agent that quoted
    `count` would over-report the engagement to the client; `top_level_count` is the honest number.
    """
    eid = _engagement(session_factory)
    parent = _finding(session_factory, eid, title="Kerberoasting")
    child_a = _finding(session_factory, eid, title="Kerberoasting — DC01", parent_id=parent,
                       order_index=1)
    child_b = _finding(session_factory, eid, title="Kerberoasting — SQL01", parent_id=parent,
                       order_index=2)

    body = client.get(f"{M}/engagements/{eid}/findings").get_json()

    assert [f["id"] for f in body["ungrouped"]] == [parent, child_a, child_b]
    assert [f["parent_id"] for f in body["ungrouped"]] == [None, parent, parent]
    assert body["count"] == 3            # board rows, children included
    assert body["top_level_count"] == 1  # what the report actually shows


def test_top_level_count_matches_what_the_renderer_produces(client, token, session_factory, app):
    """`top_level_count` is pinned to the RENDERER, not to a restatement of its rule.

    Asserted against `build_report_context`'s own output over a deliberately awkward board: a nested
    cluster, an excluded child, an excluded parent whose child therefore renders top-level, an excluded
    group, and a flat finding. Any future change to the nesting/filtering rules that this route did not
    follow makes the two numbers differ.

    STATUSES ARE PART OF THE BOARD ON PURPOSE (lotek#618). Inclusion is `include_in_report AND the
    status' disposition is not "excluded"` -- a `false_positive` leaves the deliverable even though the
    operator never unticked it. A board carrying no statuses is a board where the second half of the
    rule is unobservable, which is exactly how this assertion stayed green while `top_level_count`
    filtered on `include_in_report` alone and over-reported every false positive to the client.
    """
    from scribble.reporting import build_report_context

    eid = _engagement(session_factory)
    shown = _group(session_factory, eid, "Active Directory", order_index=0)
    hidden = _group(session_factory, eid, "Draft", order_index=1)
    parent = _finding(session_factory, eid, title="Kerberoasting", group_id=shown)
    _finding(session_factory, eid, title="— DC01", group_id=shown, parent_id=parent, order_index=1)
    _finding(session_factory, eid, title="— SQL01", group_id=shown, parent_id=parent, order_index=2,
             include_in_report=False)
    orphaned_parent = _finding(session_factory, eid, title="SMB signing", group_id=shown,
                               order_index=3, include_in_report=False)
    _finding(session_factory, eid, title="— FS01", group_id=shown, parent_id=orphaned_parent,
             order_index=4)
    # Ticked for the report, but dispositioned OUT of it: the renderer drops it, so the count must too.
    _finding(session_factory, eid, title="Anonymous FTP", group_id=shown, order_index=5,
             status=fm.FindingStatus.false_positive)
    # Remediated: still a finding the client should see, so it stays in BOTH numbers.
    _finding(session_factory, eid, title="Default credentials", group_id=shown, order_index=6,
             status=fm.FindingStatus.fixed)
    # A false-positive PARENT: its live child loses its nesting anchor and renders top-level.
    fp_parent = _finding(session_factory, eid, title="Legacy TLS", group_id=shown, order_index=7,
                         status=fm.FindingStatus.false_positive)
    _finding(session_factory, eid, title="— WEB01", group_id=shown, parent_id=fp_parent,
             order_index=8)
    _finding(session_factory, eid, title="Banner disclosure")
    _finding(session_factory, eid, title="Self-signed certificate", order_index=1,
             status=fm.FindingStatus.false_positive)
    _finding(session_factory, eid, title="Hidden section finding", group_id=hidden)
    with session_factory() as db:
        db.get(fm.FindingGroup, hidden).include_in_report = False
        db.commit()

    reported = client.get(f"{M}/engagements/{eid}/findings").get_json()["top_level_count"]

    with session_factory() as db:
        engagement = db.get(fm.Engagement, eid)
        rendered = sum(len(group.findings) for group in build_report_context(engagement).groups)
    assert reported == rendered, "the listing's top_level_count disagrees with the renderer"


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


@pytest.mark.parametrize(
    "block,payload",
    # NOTE: ``references`` is NO LONGER a prose block (#624 moved it to a typed column), so it is not
    # parametrized here — its clearing is exercised by test_patch_references_column below.
    [("description", {"description": ""}),
     ("description", {"description": "   "}),
     ("remediation", {"remediation": ""})],
)
def test_patch_clears_a_prose_block_when_the_value_is_empty(
    client, token, session_factory, block, payload
):
    """A supplied-but-EMPTY prose value CLEARS that block. It used to be silently dropped.

    `_author_content_json`'s guards are truthiness tests, right for a create ("nothing supplied" ==
    "supplied nothing") and wrong for an edit: `{"title": "x", "description": ""}` answered 200 with the
    PLACEHOLDER prose still in `content_json`, and `{"description": ""}` alone answered a misleading 400
    "no updatable fields supplied" — so there was no way at all to clear a block through this API, and the
    attempt to do so alongside another field reported success for prose that was never written. That is the
    exact failure `_patch_content_blocks`' docstring says it exists to prevent, arriving by value instead of
    by type. An empty ProseMirror doc is what the cookie editor's autosave stores for a cleared block.
    """
    eid = _engagement(session_factory)
    fid = _finding(session_factory, eid)
    with session_factory() as db:
        finding = db.get(fm.EngagementFinding, fid)
        finding.content_json = {block: schema.doc_from_text("PLACEHOLDER — replace me")}
        db.commit()

    resp = client.patch(f"{M}/findings/{fid}", json={"title": "Edited", **payload})
    assert resp.status_code == 200, resp.get_json()
    assert resp.get_json()["content_json"][block] == schema.empty_doc()

    with session_factory() as db:
        stored = db.get(fm.EngagementFinding, fid)
        assert stored.content_json[block] == schema.empty_doc(), "the block was reported cleared but is not"
        assert stored.content_html[block] in ("", None) or "PLACEHOLDER" not in str(
            stored.content_html[block]
        ), "the cached HTML still shows prose the report no longer contains"


def test_patch_clearing_a_block_alone_is_not_a_no_op_400(client, token, session_factory):
    """Clearing prose is a real edit, so it must not need a second field to be accepted. `{"description":
    ""}` on its own used to fall through to "no updatable fields supplied"."""
    eid = _engagement(session_factory)
    fid = _finding(session_factory, eid)

    resp = client.patch(f"{M}/findings/{fid}", json={"description": ""})
    assert resp.status_code == 200, resp.get_json()
    with session_factory() as db:
        assert db.get(fm.EngagementFinding, fid).content_json["description"] == schema.empty_doc()


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
    "field,cap",
    [("title", 512), ("category", 255), ("cvss_vector", 255), ("target_host", 255),
     ("target_port", 16), ("target_url", 1024)],
)
def test_patch_refuses_a_value_that_would_overflow_its_column(client, token, session_factory, field, cap):
    """An over-long value must be a 400 at the boundary, not a 500 from the database.

    These are `String(n)` columns: on Postgres — what prod runs — an over-long value raises
    `StringDataRightTruncation`, so an agent sending a long title gets a 500 for what is really a bad
    request. SQLite (this suite's backend) stores it happily, which is exactly why the cap has to be code
    rather than something "the tests would have caught": a green run here proves nothing about the column
    width. Same shape as the uuid/Integer trap that SQLite hid until real Postgres refused it.
    """
    eid = _engagement(session_factory)
    fid = _finding(session_factory, eid, title="Original")

    at_cap = client.patch(f"{M}/findings/{fid}", json={field: "x" * cap})
    assert at_cap.status_code == 200, at_cap.get_json()  # the cap itself must still be accepted

    over = client.patch(f"{M}/findings/{fid}", json={field: "x" * (cap + 1)})
    assert over.status_code == 400, over.get_json()
    assert "too long" in over.get_json()["detail"]


@pytest.mark.parametrize(
    "field,cap",
    [("title", 512), ("cvss_vector", 255), ("target_host", 255), ("target_port", 16),
     ("target_url", 1024)],
)
def test_create_refuses_a_value_that_would_overflow_its_column(
    client, token, session_factory, field, cap
):
    """The CREATE route bounds the same columns PATCH does — it did not, and that is the same 500.

    The width caps landed on PATCH only, which left the create route on the SAME blueprint writing the same
    `String(n)` columns unbounded: `POST …/findings {"title": "x"*600}` answered 201 while
    `PATCH …/findings/<id>` with the identical value answered 400. On SQLite (this suite) the create stores
    the over-long value silently; on prod Postgres it raises `StringDataRightTruncation` and the caller gets
    a 500 for what is really a bad request. A cap only one of two writers consults is not a boundary.
    """
    eid = _engagement(session_factory)
    base = {"title": "Authored finding", "severity": "high"}

    at_cap = client.post(f"{M}/engagements/{eid}/findings", json={**base, field: "x" * cap})
    assert at_cap.status_code == 201, at_cap.get_json()  # the cap itself is still accepted

    over = client.post(f"{M}/engagements/{eid}/findings", json={**base, field: "x" * (cap + 1)})
    assert over.status_code == 400, over.get_json()
    assert "too long" in over.get_json()["detail"]


@pytest.mark.parametrize(
    "body",
    [
        {"target_host": {"host": "10.0.0.1"}},   # a dict bound straight to a String column
        {"target_url": ["https://x"]},
        {"target_port": {"port": 443}},
        {"target_port": True},                   # bool is an int subclass — not a port
    ],
)
def test_create_refuses_a_wrong_typed_target_field(client, token, session_factory, body):
    """`target_host`/`target_url`/`target_port` were assigned from the body with no type check at all, so a
    dict or a list bound directly to a `String` column — a driver-level error on Postgres for a request that
    should never have reached the database."""
    eid = _engagement(session_factory)
    resp = client.post(
        f"{M}/engagements/{eid}/findings",
        json={"title": "Authored finding", "severity": "high", **body},
    )
    assert resp.status_code == 400, resp.get_json()
    assert resp.get_json()["error"] == "bad_request"


def test_create_coerces_an_integer_target_port_to_text_at_the_boundary():
    """An integer `target_port` becomes text at the PARSE, so every create branch writes a `str` to the
    `String(16)` column.

    Asserted on `_parse_target_fields` rather than on the stored row, and that is deliberate: SQLite applies
    column affinity and silently converts an int bound to a `VARCHAR` — the probe reads it back as `'8443'`
    either way — so an end-to-end assertion here could not fail and would be a guard in name only. The
    defect it guards is real and only visible on Postgres, where a raw int bound to a varchar is a
    `DataError`; the direct-author branch coerced with `str()` while the template and promote branches
    assigned `data[key]` raw, so the same field's type depended on which branch ran.
    """
    from scribble.api_pat import _parse_target_fields

    parsed, err = _parse_target_fields({"target_port": 8443, "target_host": " 10.0.0.1 "})
    assert err is None
    assert parsed == {"target_port": "8443", "target_host": "10.0.0.1"}


@pytest.mark.parametrize(
    "path,body,field,cap",
    [
        ("/engagements", {"client_id": ACME}, "name", 255),              # Engagement.name     String(255)
        ("/engagements", {"client_id": ACME, "name": "Q3"}, "scope_type", 64),
        ("/engagements", {"client_id": ACME, "name": "Q3"}, "company_name", 255),
        ("/templates", {}, "name", 512),                                 # …Template.name      String(512)
        ("/templates", {"name": "Weak TLS"}, "category", 255),
        ("/templates", {"name": "Weak TLS"}, "cvss_vector", 255),
    ],
)
def test_every_create_route_on_this_blueprint_bounds_its_strings(
    client, token, session_factory, path, body, field, cap
):
    """The two OTHER create routes write `String(n)` columns too, and they were unbounded as well.

    Found while fixing the findings-create hole the review reported: `POST /engagements` with a 5000-char
    `name` answered 201 (probe 6 of the reviewer's own script), and `scope_type`/`company_name` were read
    raw from the body — so a dict bound straight to a `String` column. Fixing one route and leaving the
    engagement-create route (the FIRST call any agent makes) with the same defect would have been fixing the
    instance instead of the class.
    """
    at_cap = client.post(f"{M}{path}", json={**body, field: "x" * cap})
    assert at_cap.status_code == 201, at_cap.get_json()

    over = client.post(f"{M}{path}", json={**body, field: "x" * (cap + 1)})
    assert over.status_code == 400, over.get_json()
    assert "too long" in over.get_json()["detail"]

    wrong_type = client.post(f"{M}{path}", json={**body, field: {"nested": 1}})
    assert wrong_type.status_code == 400, wrong_type.get_json()


ARTIFACT_FILENAME_CAP = 222  # NAME_MAX(255) - len(uuid4().hex) - len("_") — see `_ARTIFACT_FILENAME_MAX_LEN`


@pytest.mark.parametrize(
    "body,expected",
    [
        ({"filename": "x" * (ARTIFACT_FILENAME_CAP + 1)}, "too long"),
        ({"filename": {"name": "x.png"}}, "string"),    # reached mimetypes.guess_type -> 500
        ({"caption": {"text": "hi"}}, "string"),        # a dict bound to a Text column
    ],
)
def test_artifact_upload_bounds_and_types_its_strings(client, token, session_factory, body, expected):
    """The evidence-upload route is the last string writer on this blueprint, and it was unguarded too.

    `filename`/`caption` were read raw from the JSON body, so a dict reached `mimetypes.guess_type` and the
    `caption` `Text` column respectively — a 500 for a request that should never have reached either. An
    over-long `filename` is a 500 as well: `save_bytes` stores the bytes as `<uuid4hex>_<secure_filename>`,
    so the basename passes `NAME_MAX` at 223 caller-supplied characters and the write raises
    `ENAMETOOLONG` (`Artifact.filename` is `String(512)`, so the column would truncate later still — the
    filesystem is the binding limit, which is why the cap is 222 and not 512). Same class as the
    create-route caps, found by asking which OTHER routes on this blueprint write a string.
    """
    eid = _engagement(session_factory)
    resp = client.post(
        f"{M}/engagements/{eid}/artifacts",
        json={"filename": "evidence.png", "content_base64": base64.b64encode(PNG).decode(), **body},
    )
    assert resp.status_code == 400, resp.get_json()
    assert expected in resp.get_json()["detail"]


def test_artifact_upload_accepts_a_filename_AT_the_cap(client, token, session_factory):
    """The cap has to be the number the filesystem actually enforces, so assert the accepted side too.

    Without this half the guard passes with ANY cap — including one above the real limit, which refuses
    nothing that was failing and leaves every name between the two numbers still answering 500. The cap
    was 512 (the column width) until this test was written and a 300-character name still 500'd.
    """
    eid = _engagement(session_factory)
    resp = client.post(
        f"{M}/engagements/{eid}/artifacts",
        json={
            "filename": "x" * ARTIFACT_FILENAME_CAP,
            "content_base64": base64.b64encode(PNG).decode(),
        },
    )
    assert resp.status_code == 201, resp.get_json()


def test_artifact_upload_stores_a_unicode_filename_that_secure_filename_EXPANDS(
    client, token, session_factory, app
):
    """A name UNDER the 222 cap that `secure_filename` makes LONGER must still store, not 500.

    The cap counts the CALLER's characters; `artifacts_storage.save_bytes` writes
    `"<uuid4hex>_" + secure_filename(name)`, and `secure_filename` NFKD-normalizes — which EXPANDS: "½"
    becomes "1⁄2" -> "12". So 200 "½" plus ".png" is 204 characters, passes the cap, and came out 404
    characters long: `OSError: [Errno 36] File name too long`, a **500** with no artifact stored, reachable
    from any write-scoped PAT and from the cookie upload too.

    Fixed in `save_bytes` rather than at the API boundary, because that is the layer that knows the final
    name (and the only one the cookie path also goes through) — the API cap stays as the fast 400. Asserted on
    the stored basename against the real `NAME_MAX`, so a cap that is merely *different* from the filesystem's
    would not pass: the bytes have to be on disk and the extension has to survive.
    """
    eid = _engagement(session_factory)
    name = "\u00bd" * 200 + ".png"
    assert len(name) <= ARTIFACT_FILENAME_CAP  # the caller's characters DO fit — that is the trap
    resp = client.post(
        f"{M}/engagements/{eid}/artifacts",
        json={"filename": name, "content_base64": base64.b64encode(PNG).decode()},
    )
    assert resp.status_code == 201, resp.get_json()
    with session_factory() as db:
        artifact = db.get(fm.Artifact, resp.get_json()["id"])
        evidence_ref, stored_name = artifact.storage_path, artifact.filename
    # Evidence is keyed by object id now, so no filesystem name is derived from the caller's and the
    # ENAMETOOLONG failure this guarded is structurally gone. The row still keeps the caller's name.
    assert read_evidence(app, evidence_ref) is not None
    assert stored_name.endswith(".png")


def test_group_name_that_would_overflow_its_column_is_refused(client, token, session_factory):
    """`FindingGroup.name` is String(128) — same reasoning as the finding fields above, on both the create
    and the rename path."""
    eid = _engagement(session_factory)
    assert client.post(f"{M}/engagements/{eid}/groups", json={"name": "x" * 128}).status_code == 201
    over = client.post(f"{M}/engagements/{eid}/groups", json={"name": "x" * 129})
    assert over.status_code == 400 and "too long" in over.get_json()["detail"]

    gid = _group(session_factory, eid, "Web")
    renamed = client.patch(f"{M}/engagements/{eid}/groups/{gid}", json={"name": "y" * 129})
    assert renamed.status_code == 400 and "too long" in renamed.get_json()["detail"]
    with session_factory() as db:
        assert db.get(fm.FindingGroup, gid).name == "Web"


@pytest.mark.parametrize(
    "body",
    [
        {},                              # nothing to do — say so rather than 200 for a no-op
        {"title": "   "},                # a blank title would silently keep the old one on the cookie form
        {"severity": "catastrophic"},
        {"cvss_score": 11},              # outside the CVSS range
        {"cvss_score": -1},
        {"cvss_score": float("inf")},    # Python's JSON parser accepts Infinity/NaN; the column would too
        {"confidence": 5},
        {"status": "wontfix"},
        {"cvss_score": "high"},
        {"include_in_report": "yes"},
        {"category": {"nested": 1}},
        {"content_json": "not-an-object"},
        # …and the same check ONE LEVEL DOWN. The container's type was guarded and its VALUES were not, so
        # each of these answered 200 after replacing that block's prose with an empty doc.
        {"content_json": {"description": "Updated description text"}},
        {"content_json": {"description": {"type": "paragraph", "content": []}}},
        {"content_json": {"impact": 42}},
        {"content_json": {"description": ["a", "b"]}},
        {"content_json": {"description": None}},
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


def test_patch_refuses_a_non_doc_block_INSTEAD_of_emptying_the_prose(client, token, session_factory):
    """A `content_json` value that is not a ProseMirror doc must be REFUSED, not silently stored as empty.

    `sanitize_prosemirror` replaces any non-`doc` root with `schema.empty_doc()` — deliberately, so an
    untrusted caller cannot smuggle a non-doc root past the walker. Validating only that `content_json` was a
    dict therefore made this route answer **200** for `{"content_json": {"description": "Updated text"}}`
    after overwriting the vuln write-up with `{"type": "doc", "content": []}`, and echo the emptied doc back
    as if that were the edit: a client's authored prose destroyed, irreversibly, by the likeliest mistake an
    agent makes here (`content_json` is the only way to write a non-default block, and this same route takes
    `description` as plain TEXT one key over).

    The cookie twin is asserted alongside on the SAME input, because "the two surfaces must not diverge" is
    the branch's own principle and this is where they did: `autosave_api.autosave_block` gates on
    `schema.is_doc` and answers 400 for exactly this body.
    """
    eid = _engagement(session_factory)
    fid = _finding(session_factory, eid)
    with session_factory() as db:
        before = db.get(fm.EngagementFinding, fid).content_json["description"]
    assert schema.plain_text(before) == "original prose"

    resp = client.patch(f"{M}/findings/{fid}", json={"content_json": {"description": "Updated text"}})
    assert resp.status_code == 400, resp.get_json()
    assert "ProseMirror doc" in resp.get_json()["detail"]
    assert "description" in resp.get_json()["detail"]

    # The cookie route, same value, same verdict.
    twin = client.post(f"/scribble/api/findings/{fid}/blocks/description", json="Updated text")
    assert twin.status_code == 400, twin.get_json()

    with session_factory() as db:
        # Nothing was written: the prose is byte-identical to what the author had.
        assert db.get(fm.EngagementFinding, fid).content_json["description"] == before


def test_patch_still_accepts_a_real_prosemirror_doc_for_a_NON_default_block(
    client, token, session_factory
):
    """The accepted side of the guard above — without it the refusal could be over-broad and pass anyway.

    `impact` is not one of the default blocks and has no plain-text twin on this route, so `content_json` is
    the ONLY way to author it: if the new check refused a well-formed doc under a custom block name, the
    guard would have closed the loss by closing the feature.
    """
    eid = _engagement(session_factory)
    fid = _finding(session_factory, eid)
    resp = client.patch(
        f"{M}/findings/{fid}",
        json={"content_json": {"impact": schema.doc_from_text("Full domain compromise.")}},
    )
    assert resp.status_code == 200, resp.get_json()
    with session_factory() as db:
        stored = db.get(fm.EngagementFinding, fid).content_json
    assert schema.plain_text(stored["impact"]) == "Full domain compromise."
    assert schema.plain_text(stored["description"]) == "original prose"  # untouched


def test_create_routes_refuse_a_non_doc_block_too(client, token, session_factory):
    """The same element check on the CREATE routes, because a 201 for prose that was never stored is the
    same silent success as a 200 — and a cap/guard that only one of two writers consults is not a boundary
    (the lesson `_COLUMN_MAX_LEN` is named for)."""
    eid = _engagement(session_factory)
    finding = client.post(
        f"{M}/engagements/{eid}/findings",
        json={"title": "Authored", "severity": "high", "content_json": {"description": "raw text"}},
    )
    assert finding.status_code == 400 and "ProseMirror doc" in finding.get_json()["detail"]

    template = client.post(
        f"{M}/templates", json={"name": "T", "content_json": {"description": "raw text"}}
    )
    assert template.status_code == 400 and "ProseMirror doc" in template.get_json()["detail"]

    with session_factory() as db:
        assert db.scalars(select(fm.EngagementFinding)).all() == []


def test_every_content_writer_bounds_the_content_json_BLOCK_COUNT(client, token, session_factory):
    """`content_json`'s block COUNT is bounded, on every route that writes it.

    The gap this closes, measured on this branch before the cap: a 204 KB `PATCH` carrying 5,000
    `content_json` blocks answered **200**, ran `render_block` 5,000 times, and PERSISTED 5,001 blocks —
    after which every later render of that finding (report HTML, docx, editor preview, on both the cookie
    and machine surfaces) walked all of them again. The id-list cap review round 2 added bounds work that
    ends with the request; this bounds work that outlives it, in a client's deliverable.

    Swept over all three writers deliberately: `_COLUMN_MAX_LEN` and `_non_doc_blocks_error` both shipped
    covering one writer while a sibling on the same blueprint wrote the same column unguarded, and each
    time the guard the commit existed for was still reachable one route over.
    """
    eid = _engagement(session_factory)
    fid = _finding(session_factory, eid)
    with session_factory() as db:
        templates_before = len(db.scalars(select(fm.VulnerabilityTemplate)).all())
    over = {f"b{i}": {"type": "doc", "content": []} for i in range(65)}

    calls = [
        ("PATCH", f"{M}/findings/{fid}", {"content_json": over}),
        ("POST", f"{M}/engagements/{eid}/findings",
         {"title": "Authored", "severity": "high", "content_json": over}),
        ("POST", f"{M}/templates", {"name": "T", "content_json": over}),
    ]
    unbounded = []
    for method, url, body in calls:
        resp = client.open(url, method=method, json=body)
        if resp.status_code != 400 or "at most 64 blocks" not in resp.get_json().get("detail", ""):
            unbounded.append((method, url, resp.status_code, resp.get_json()))
    assert unbounded == [], f"a writer accepted an over-cap content_json: {unbounded}"

    # …and nothing was stored by any of the three refusals. Counted against a BEFORE snapshot, not
    # against zero: `seed_defaults` ships a vuln-template library, so an == [] assertion here would fail
    # for a reason that has nothing to do with the guard.
    with session_factory() as db:
        assert len(db.get(fm.EngagementFinding, fid).content_json) == 1  # the seeded "description"
        assert len(db.scalars(select(fm.EngagementFinding)).all()) == 1  # no finding was created
        assert len(db.scalars(select(fm.VulnerabilityTemplate)).all()) == templates_before


def test_every_content_writer_bounds_the_REFERENCES_LIST(client, token, session_factory):
    """The same, for `references` — the other input whose length costs per-element work that persists.

    Measured before the cap: `PATCH {"references": [<200,000 urls>]}` answered **200** and stored
    **22.2 MB** into ONE finding. `references` is `str()`-coerced/coerced-to-a-value-object per element,
    so the count has to be refused before the list is walked. (#624 moved a finding's references to a
    typed column; the FINDING-template `references` list on POST /templates is still a column too — both
    still bounded by ``_REFERENCE_LIST_MAX``.)
    """
    eid = _engagement(session_factory)
    fid = _finding(session_factory, eid)
    with session_factory() as db:
        templates_before = len(db.scalars(select(fm.VulnerabilityTemplate)).all())
    over = ["http://example.com/cve"] * 501

    calls = [
        ("PATCH", f"{M}/findings/{fid}", {"references": over}),
        ("POST", f"{M}/engagements/{eid}/findings",
         {"title": "Authored", "severity": "high", "references": over}),
        ("POST", f"{M}/templates", {"name": "T", "references": over}),
    ]
    unbounded = []
    for method, url, body in calls:
        resp = client.open(url, method=method, json=body)
        if resp.status_code != 400 or "at most 500 entries" not in resp.get_json().get("detail", ""):
            unbounded.append((method, url, resp.status_code, resp.get_json()))
    assert unbounded == [], f"a writer accepted an over-cap references list: {unbounded}"

    with session_factory() as db:
        assert schema.plain_text(
            db.get(fm.EngagementFinding, fid).content_json["description"]
        ) == "original prose"
        assert "references" not in db.get(fm.EngagementFinding, fid).content_json
        assert len(db.scalars(select(fm.EngagementFinding)).all()) == 1  # no finding was created
        assert len(db.scalars(select(fm.VulnerabilityTemplate)).all()) == templates_before


def test_a_body_AT_the_content_caps_is_ACCEPTED_and_stored(client, token, session_factory):
    """The accepted side, asserted because a cap test without it can hold the WRONG number and still pass.

    That is not hypothetical here: the artifact-filename cap shipped at a value one session had chosen
    without an accepted-side assertion, and the next session found it wrong precisely by adding this half
    (see the plan's §24). Exactly AT each cap must store — a boundary that is off by one refuses a
    legitimate deliverable, which is the failure a length guard is most likely to introduce.
    """
    eid = _engagement(session_factory)
    fid = _finding(session_factory, eid)
    at_cap = {f"b{i}": schema.doc_from_text(f"block {i}") for i in range(64)}
    resp = client.patch(f"{M}/findings/{fid}", json={"content_json": at_cap})
    assert resp.status_code == 200, resp.get_json()

    # 500 references AT the cap is accepted (a boundary that refuses exactly-500 would drop a legitimate
    # deliverable). #624: references land on the typed column, NOT a content block.
    resp = client.patch(f"{M}/findings/{fid}", json={"references": ["http://x/cve"] * 500})
    assert resp.status_code == 200, resp.get_json()

    with session_factory() as db:
        finding = db.get(fm.EngagementFinding, fid)
        stored = finding.content_json
        assert "references" not in stored  # references are a typed column now, not a prose block
        assert finding.references  # the column was written
    assert len(stored) == 65  # 64 authored + the seeded "description" (references is a column, not a block)
    assert schema.plain_text(stored["b63"]) == "block 63"


def test_patch_authors_structured_references_and_metadata(client, token, session_factory):
    """PATCH authors the #624/#625 typed columns: references (add/edit/SUPPRESS), cve_ids/cwe_ids/
    owasp_categories (normalized), and threat_intel (CLEAR-only). The response echoes them back."""
    eid = _engagement(session_factory)
    fid = _finding(session_factory, eid, cve_ids=["CVE-2020-0001"], threat_intel={"as_of": "x",
                   "source": "s", "cves": {"CVE-2020-0001": {"kev": True}}})

    resp = client.patch(f"{M}/findings/{fid}", json={
        "references": ["https://vendor/adv",
                       {"label": "noisy", "url": "https://noisy/1", "suppressed": True}],
        "cve_ids": ["cve-2021-44228", "CVE-2021-44228"],   # normalized + deduped
        "cwe_ids": ["79"],                                  # bare number -> CWE-79
        "owasp_categories": ["A03:2021", "A99:2021"],       # unknown id dropped
    })
    assert resp.status_code == 200, resp.get_json()
    body = resp.get_json()
    assert [r["label"] for r in body["references"]] == ["https://vendor/adv", "noisy"]
    assert body["references"][0]["source"] == "author"
    assert body["references"][1]["suppressed"] is True
    assert body["cve_ids"] == ["CVE-2021-44228"]
    assert body["cwe_ids"] == ["CWE-79"]
    assert body["owasp_categories"] == ["A03:2021"]        # A99 dropped

    # threat_intel is enrichment-managed: a non-null value is refused, null clears it.
    bad = client.patch(f"{M}/findings/{fid}", json={"threat_intel": {"as_of": "y", "cves": {}}})
    assert bad.status_code == 400 and "enrichment-managed" in bad.get_json()["detail"]
    cleared = client.patch(f"{M}/findings/{fid}", json={"threat_intel": None})
    assert cleared.status_code == 200
    with session_factory() as db:
        assert db.get(fm.EngagementFinding, fid).threat_intel is None


@pytest.mark.parametrize("field", ["title", "analyst_notes", "target_host"])
def test_patch_escapes_a_NUL_byte_that_postgres_would_refuse(client, token, session_factory, field):
    """A NUL (0x00) in an accepted string is ESCAPED at the boundary, mirroring core's `nul_safe`.

    Postgres refuses a bind containing NUL outright (`psycopg`: "A string literal cannot contain NUL (0x00)
    characters"), so on prod this was a **500** for what is a bad request — while this suite, on SQLite,
    stored it and answered 200. That is the same blindness the length caps were added for, one class over:
    SQLite cannot see either failure, so both have to be checked in code. Scan output is where a stray NUL
    comes from, and `analyst_notes` is the widest door (`Text`, no length cap at all).

    Escaped rather than deleted or refused, exactly as core does it: the count survives in the marker, the
    substitution is visible in the report, and an agent is not blocked from filing a finding over a byte it
    does not control. Asserted on the STORED value, so this test is about what reaches the column.
    """
    eid = _engagement(session_factory)
    fid = _finding(session_factory, eid)
    resp = client.patch(f"{M}/findings/{fid}", json={field: "scan\x00banner"})
    assert resp.status_code == 200, resp.get_json()
    with session_factory() as db:
        stored = getattr(db.get(fm.EngagementFinding, fid), field)
    assert "\x00" not in stored, stored
    assert stored.startswith("scan\u2400banner")
    assert "1 NUL byte replaced" in stored


# ── 3. delete ────────────────────────────────────────────────────────────────────────────────────────


def test_delete_finding_takes_its_evidence_rows_and_files(client, token, session_factory, app):
    """A finding IS its content, so deleting it takes its artifacts — DB rows AND the bytes on disk.

    Without the explicit cascade `findings_service.delete_finding` performs, a bare ORM delete would NULL
    each artifact's nullable `finding_id` instead: the row survives, orphaned, and the file leaks.
    """
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
        evidence_ref = db.get(fm.Artifact, artifact_id).storage_path
    assert read_evidence(app, evidence_ref) is not None

    resp = client.delete(f"{M}/findings/{fid}")
    assert resp.status_code == 200, resp.get_json()
    assert resp.get_json() == {
        "deleted": True, "finding_id": fid, "engagement_id": eid, "detached_children": [],
    }

    with session_factory() as db:
        assert db.get(fm.EngagementFinding, fid) is None
        assert db.get(fm.Artifact, artifact_id) is None
    assert read_evidence(app, evidence_ref) is None


def test_delete_a_parent_detaches_its_children_instead_of_violating_the_self_FK(
    client, token, session_factory, app
):
    """Deleting a PROMOTED PARENT must succeed, keep its per-host children, and say that it did.

    This was a 500 — the single operation ext#41 was filed about, on the shape this very API produces by
    default. `EngagementFinding.parent_id` is a self-FK with no `ondelete` and no ORM relationship, so
    nothing cleared it: the DELETE raised `IntegrityError` (SQLite) / `ForeignKeyViolation` (prod
    Postgres), nothing was deleted, and the audit row rolled back with it. `promote_job` builds exactly
    this shape for every finding that resolves to a vuln-DB template, so "an agent's only recovery is
    delete-and-recreate" still could not be performed on a promoted finding.

    The children are DETACHED, never deleted: the parent is a synthesized umbrella row over the template's
    write-up, while each child carries the irreplaceable per-host evidence — its own target, variables and
    artifacts. One DELETE must not destroy N findings the caller never named. The child's artifact below is
    the load-bearing assertion: it proves the cascade stopped at the parent.
    """
    eid = _engagement(session_factory)
    gid = _group(session_factory, eid, "Active Directory")
    parent = _finding(session_factory, eid, title="Kerberoasting", group_id=gid)
    child_a = _finding(session_factory, eid, title="Kerberoasting — DC01", group_id=gid,
                       parent_id=parent, target_host="10.0.0.10", order_index=1)
    child_b = _finding(session_factory, eid, title="Kerberoasting — SQL01", group_id=gid,
                       parent_id=parent, target_host="10.0.0.20", order_index=2)
    upload = client.post(
        f"{M}/engagements/{eid}/artifacts",
        json={"filename": "hashes.png", "content_base64": base64.b64encode(PNG).decode(),
              "finding_id": child_a},
    )
    assert upload.status_code == 201, upload.get_json()
    child_artifact = upload.get_json()["id"]
    with session_factory() as db:
        child_ref = db.get(fm.Artifact, child_artifact).storage_path

    resp = client.delete(f"{M}/findings/{parent}")
    assert resp.status_code == 200, resp.get_json()
    assert resp.get_json() == {
        "deleted": True, "finding_id": parent, "engagement_id": eid,
        "detached_children": [child_a, child_b],
    }

    with session_factory() as db:
        assert db.get(fm.EngagementFinding, parent) is None
        for cid in (child_a, child_b):
            child = db.get(fm.EngagementFinding, cid)
            assert child is not None, "a per-host child must survive its parent's delete"
            assert child.parent_id is None, "a surviving child must be detached, not dangling"
            assert str(child.group_id) == gid  # stays where the board already showed it
        assert db.get(fm.Artifact, child_artifact) is not None
    assert read_evidence(app, child_ref) is not None, \
        "the child's evidence must not be swept up by the parent's delete"


def test_deleting_a_parent_is_recorded_with_the_children_it_detached(
    client, token, session_factory, app
):
    """The audit row for a parent delete names the rows it re-parented. A trail recording only "one finding
    deleted" would hide the fact that two other findings changed shape in the same transaction."""
    recorded = _install_audit_recorder(app)
    eid = _engagement(session_factory)
    parent = _finding(session_factory, eid, title="Weak TLS")
    child = _finding(session_factory, eid, title="Weak TLS — web01", parent_id=parent)

    assert client.delete(f"{M}/findings/{parent}").status_code == 200

    row = next(r for r in recorded if r[0] == "ext:scribble:delete_finding")
    # The audit row carries the raw ORM ids (uuid.UUID), not their JSON-string form -- stringify to
    # compare against the fixture id.
    assert [str(cid) for cid in row[4]["detached_children"]] == [child]


def test_delete_finding_clears_every_row_that_references_it(client, token, session_factory, app):
    """DELETE must succeed for a finding referenced by ANY of the six FK columns that point at it.

    `detach_children` fixed ONE of them (`parent_id`) and the docstring claimed the cascade was complete, so
    `DELETE /findings/<id>` — the headline capability of ext#41 — still answered **500**
    (`sqlite3.IntegrityError` here, `ForeignKeyViolation` on prod Postgres; transaction rolled back, finding
    not deleted, audit row lost) for a whole class of findings. Adversarial review round 2 reproduced three:

    * a `CollabDoc`, which the live co-editing room's `_persist_room` writes the moment a HUMAN opens that
      block in the browser — i.e. any finding anybody has edited in the editor,
    * an `EngagementChecklistItem.finding_id`, set through the supported checklist route,
    * a finding-scoped `VariableValue`, which promotion writes per host.

    So this asserts the whole set in one go, and each disposition separately, because "it returns 200" is not
    the property — WHICH rows died is. Owned state goes (the CRDT blob, the per-finding variable binding, the
    artifact + its bytes); the checklist item SURVIVES with `finding_id` NULL, because a coverage checklist
    must not lose an item just because the finding documenting it was deleted; the child is detached, not
    deleted. `FindingTag` is here to VERIFY, not to assume, that the ORM's `secondary` cascade on
    `EngagementFinding.tags` really does remove the association row — it is the one referrer that was already
    safe, and the only way to know that is to assert it.
    """
    eid = _engagement(session_factory)
    fid = _finding(session_factory, eid, title="Parent")
    child_id = _finding(session_factory, eid, title="10.0.0.5", parent_id=fid)

    upload = client.post(
        f"{M}/engagements/{eid}/artifacts",
        json={"filename": "evidence.png", "content_base64": base64.b64encode(PNG).decode(),
              "finding_id": fid},
    )
    assert upload.status_code == 201, upload.get_json()
    artifact_id = upload.get_json()["id"]

    with session_factory() as db:
        db.add(fm.CollabDoc(finding_id=fid, block="description", ydoc_state=b"\x00\x01ydoc"))
        tag = fm.Tag(name="internal")
        variable = fm.TemplateVariable(key="PER_FINDING_HOST", label="Host")  # not a seeded key
        checklist = fm.EngagementChecklist(engagement_id=eid, name="Coverage")
        db.add_all([tag, variable, checklist])
        db.flush()
        db.add_all([
            fm.FindingTag(finding_id=fid, tag_id=tag.id),
            fm.VariableValue(variable_id=variable.id, finding_id=fid, value="10.0.0.5"),
            fm.EngagementChecklistItem(
                engagement_checklist_id=checklist.id, text="SMB signing reviewed", finding_id=fid
            ),
        ])
        db.commit()
        item_id = db.scalars(select(fm.EngagementChecklistItem.id)).one()
        evidence_ref = db.get(fm.Artifact, artifact_id).storage_path

    resp = client.delete(f"{M}/findings/{fid}")
    assert resp.status_code == 200, resp.get_json()
    assert resp.get_json() == {
        "deleted": True, "finding_id": fid, "engagement_id": eid, "detached_children": [child_id],
    }

    with session_factory() as db:
        assert db.get(fm.EngagementFinding, fid) is None
        # Owned state died with it — rows AND, for the artifact, the bytes.
        assert db.scalars(select(fm.CollabDoc)).all() == []
        assert db.scalars(select(fm.VariableValue)).all() == []
        assert db.scalars(select(fm.FindingTag)).all() == []
        assert db.get(fm.Artifact, artifact_id) is None
        # The cross-link survived, unlinked: the checklist keeps its item.
        item = db.get(fm.EngagementChecklistItem, item_id)
        assert item is not None and item.finding_id is None
        # The child survived, detached and top-level.
        child = db.get(fm.EngagementFinding, child_id)
        assert child is not None and child.parent_id is None
    assert read_evidence(app, evidence_ref) is None


def test_every_column_referencing_a_finding_has_a_declared_delete_disposition():
    """The GUARD: enumerate the FK set from the metadata and fail when a member is unclassified.

    Both blocking findings of review round 2 were the same omission — a guard checked against ONE member of a
    set (`parent_id`) rather than against the set. This is the check that makes the seventh referrer loud
    instead of a prod 500: add a column pointing at `scribble_findings.id` and this test fails until
    `findings_service` says which of DELETE / NULL / handled-elsewhere it is.

    Deliberately derived from `Base.metadata` rather than from a hand-written list of tables, so it sees a new
    column the moment the model declares it — a hand-written list would be the same class of defect it exists
    to catch.
    """
    from scribble import findings_service as svc
    from scribble.models import Base

    referrers = {
        (table.name, column.name)
        for table in Base.metadata.tables.values()
        for column in table.columns
        for fk in column.foreign_keys
        if fk.column.table.name == "scribble_findings" and fk.column.name == "id"
    }
    assert len(referrers) == 7, referrers  # pinned: a change here is a schema change, read it
    # (7th: scribble_retests.finding_id, lotek#621 — retests die with the finding via the
    #  EngagementFinding.retests delete-orphan cascade; classified in _FINDING_FK_HANDLED_ELSEWHERE.)

    declared = (
        {(m.__tablename__, "finding_id") for m in svc._FINDING_OWNED_STATE}
        | {(m.__tablename__, "finding_id") for m in svc._FINDING_CROSSLINKS}
        | set(svc._FINDING_FK_HANDLED_ELSEWHERE)
    )
    assert referrers == declared, (
        "a column referencing scribble_findings.id has no delete disposition: "
        f"{referrers ^ declared} — classify it in findings_service (DELETE with the finding, NULL the link, "
        "or document who already handles it) or deleting a finding will 500 on Postgres"
    )


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
        assert [str(f.id) for f in sorted(group.findings, key=lambda f: f.order_index)] == [fid, existing]


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
        assert str(db.get(fm.EngagementFinding, fid).group_id) == home  # still where it was


def test_the_group_refusal_is_identical_for_foreign_and_nonexistent(client, token, session_factory):
    """One message for both, so the refusal confirms nothing about which group ids exist."""
    mine = _engagement(session_factory)
    theirs = _engagement(session_factory, name="Theirs")
    foreign_group = _group(session_factory, theirs, "Theirs")
    fid = _finding(session_factory, mine)

    foreign = client.post(f"{M}/findings/{fid}/move", json={"group_id": foreign_group})
    # A well-formed id with no row anywhere -- NOT a malformed one (lotek#335: group ids are UUIDs now,
    # so an int literal here would hit the parse-error 400 path instead of the not-found 404 this test
    # is actually about).
    missing = client.post(f"{M}/findings/{fid}/move", json={"group_id": str(uuid.uuid7())})
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
        assert [str(f.id) for f in ordered] == [b, c, a]
        assert [f.order_index for f in ordered] == [0, 1, 2]  # no gaps, no duplicates


def test_bulk_move_reports_the_order_index_it_actually_persisted(client, token, session_factory):
    """The `moved[]` entries must be the PERSISTED positions, not a mid-loop reading.

    Each placement reindexes the WHOLE destination, so an `order_index` read inside the placement loop can
    be stale by the time the next finding lands. The ungrouped bucket is where it bites hardest: it has no
    `order_mode` to flip to manual, so every insert re-sorts by severity — move a `low` and then a `medium`
    into a bucket that already holds a `critical`, and the `low` that was written at slot 0 ends up at slot
    2. The response said 0. A caller computing its next `order_index` from that body is working from a
    number that was never in the database.
    """
    eid = _engagement(session_factory)
    gid = _group(session_factory, eid, "Web Application")
    _finding(session_factory, eid, title="Critical (already ungrouped)", severity=Severity.critical)
    low = _finding(session_factory, eid, title="Low", severity=Severity.low, group_id=gid)
    medium = _finding(session_factory, eid, title="Medium", severity=Severity.medium, group_id=gid)

    body = client.post(
        f"{M}/engagements/{eid}/findings/move",
        json={"finding_ids": [low, medium], "group_id": None, "order_index": 0},
    ).get_json()

    with session_factory() as db:
        persisted = {
            str(f.id): f.order_index
            for f in db.scalars(
                select(fm.EngagementFinding).where(fm.EngagementFinding.engagement_id == eid)
            )
        }
    reported = {m["finding_id"]: m["order_index"] for m in body["moved"]}
    assert reported == {fid: persisted[fid] for fid in reported}, (
        f"reported {reported}, database holds {persisted}"
    )


@pytest.mark.parametrize("order_index", [-1, -5])
@pytest.mark.parametrize("bulk", [False, True])
def test_move_refuses_a_negative_order_index(client, token, session_factory, order_index, bulk):
    """A negative `order_index` is a 400, on both move routes, and nothing moves.

    `place_finding` clamps with `max(0, min(requested, len))`, so every negative offset collapsed to slot 0
    — and in a bulk move each successive insert at slot 0 pushed the previous one down, silently REVERSING
    the caller's listed order while the route answered 200 and its docstring promised the order was
    preserved. `order_index: -1` ("insert before the first", an obvious thing for an agent doing index
    arithmetic) reversed a 2-item move. Zero already means "before the first", so a negative index cannot
    express anything a caller could have meant: refusing it is the honest answer, where clamping quietly did
    the opposite of what was asked.
    """
    eid = _engagement(session_factory)
    gid = _group(session_factory, eid, "Web Application")
    a = _finding(session_factory, eid, title="A")
    b = _finding(session_factory, eid, title="B")
    c = _finding(session_factory, eid, title="C")

    if bulk:
        resp = client.post(f"{M}/engagements/{eid}/findings/move",
                           json={"finding_ids": [a, b, c], "group_id": gid,
                                 "order_index": order_index})
    else:
        resp = client.post(f"{M}/findings/{a}/move",
                           json={"group_id": gid, "order_index": order_index})

    assert resp.status_code == 400, resp.get_json()
    assert resp.get_json()["error"] == "bad_request"
    with session_factory() as db:
        assert [f.group_id for f in db.scalars(
            select(fm.EngagementFinding).where(fm.EngagementFinding.engagement_id == eid)
        )] == [None, None, None], "a refused move must not have moved anything"


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


def test_bulk_move_refuses_an_unbounded_id_list_and_pre_checks_in_ONE_query(
    client, token, session_factory, app
):
    """`finding_ids` is capped, and the membership pre-check costs one query however many ids arrive.

    The uncapped version did one `db.get` per id BEFORE it could refuse: 20,000 nonexistent ids measured
    **12.7s** of database work on SQLite (~0.64ms/id), and `MAX_CONTENT_LENGTH` defaults to 256 MiB on the
    host, so a ~7 MB body carried ~1M ids — ten minutes of a gevent worker and its DB connection, for a
    write-scoped PAT with membership on ONE engagement. This branch built a whole boundary layer for
    unbounded input and left the one list that costs a query per element uncapped.

    Both halves are asserted, because either alone leaves the amplification: the cap (a 400 before the list is
    walked) and the query count (asserted by counting real statements, so the pre-check cannot quietly go back
    to N round trips). The `order` list on the group reorder is capped by the same constant — cheap per
    element is not a bound.
    """
    from sqlalchemy import event

    eid = _engagement(session_factory)
    over = client.post(
        f"{M}/engagements/{eid}/findings/move",
        json={"finding_ids": list(range(1, 502)), "group_id": None},
    )
    assert over.status_code == 400, over.get_json()
    assert "at most 500" in over.get_json()["detail"]

    reorder = client.post(
        f"{M}/engagements/{eid}/groups/reorder", json={"order": list(range(1, 502))}
    )
    assert reorder.status_code == 400 and "at most 500" in reorder.get_json()["detail"]

    # …and the query count, on a payload that DISCRIMINATES. 20 real ids followed by one that does not
    # exist: the per-id version walks all 21 before it can refuse (21 SELECTs), the single-query version
    # refuses after one. A list of ids that are ALL missing would prove nothing — the old loop returned on
    # the first one, so it too cost exactly one query. (The first version of this assertion made that
    # mistake and stayed green when the per-id loop was restored, which is why the payload is spelled out.)
    real_ids = [_finding(session_factory, eid, title=f"F{n}", order_index=n) for n in range(20)]
    selects: list[str] = []

    @event.listens_for(app.extensions["scribble"].engine, "before_cursor_execute")
    def _count(conn, cursor, statement, params, context, executemany):  # noqa: ARG001
        if statement.lstrip().upper().startswith("SELECT") and "scribble_findings" in statement:
            selects.append(statement)

    missing = client.post(
        f"{M}/engagements/{eid}/findings/move",
        # A well-formed id with no row -- NOT a malformed one (lotek#335: an int literal here would hit
        # the parse-error 400 path before the membership pre-check this assertion is actually about).
        json={"finding_ids": [*real_ids, str(uuid.uuid7())], "group_id": None},
    )
    event.remove(app.extensions["scribble"].engine, "before_cursor_execute", _count)
    assert missing.status_code == 404, missing.get_json()
    assert len(selects) == 1, f"{len(selects)} SELECTs on scribble_findings, expected 1"

    # …and nothing moved: the refusal happens before any placement.
    with session_factory() as db:
        assert [f.order_index for f in db.scalars(
            select(fm.EngagementFinding).where(fm.EngagementFinding.id.in_(real_ids)).order_by(
                fm.EngagementFinding.id)
        ).all()] == list(range(20))


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
    # A well-formed id with no row -- NOT a malformed one (lotek#335: an int literal here fails to MATCH
    # the `<uuid:...>` route at all, so Werkzeug's own 404 page answers instead of the view's JSON 404,
    # which is a different assertion than the one this test is about).
    missing = client.open(template.format(M=M, fid=uuid.uuid7()), method=method, json=body)
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
    install_scope_enforcing_gate(app, stub_host)
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
    install_scope_enforcing_gate(app, stub_host)
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
