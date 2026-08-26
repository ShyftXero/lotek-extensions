"""ext#48 — embed a vector attack-path diagram into the scribble HTML report.

The hard requirement is backward compatibility: "reports without a diagram render identically". That
guarantee rests on three things working together — ``ReportContext.diagrams`` defaults empty,
``render_html._render_diagrams`` returns ``""`` when there is nothing to show, and
``render_html._render_document`` filters empty blocks out of the join — so this file pins all three with
a real red-then-green guard (see ``test_removing_the_empty_short_circuit_breaks_backward_compat`` below),
not just a green assertion that could pass for the wrong reason.

Three cases:
  1. BACKWARD-COMPAT — no linked diagram -> the document is byte-identical to a report with no notion of
     diagrams at all (no "Attack Paths" section, no ``sec-diagrams`` anchor, existing structure intact).
  2. WITH-DIAGRAM — a linked diagram renders a sandboxed iframe carrying the escaped snapshot + caption,
     positioned after Findings and before Methodology.
  3. PAT ENDPOINT round-trip — POST then GET ``/scribble/machine/engagements/<id>/attack-paths``.
"""

from __future__ import annotations

from scribble.content import schema
from scribble.enums import Severity
from scribble.models import Client, Engagement, EngagementDiagram, EngagementFinding, FindingGroup
from scribble.reporting import build_report_context
from scribble.reporting.render_html import render_report_html
from tests.conftest import StubActor

M = "/scribble/machine"

# A tiny "self-contained" snapshot standing in for vector's export.html — carries a script (so the
# sandboxed-iframe contract is meaningful) and an attribute-breaking quote (so escaping is meaningful).
SNAPSHOT_HTML = (
    '<!doctype html><html><body><svg id="graph"></svg>'
    '<script>document.title="pwned\\"";</script>'
    "</body></html>"
)


def _block(text: str) -> dict:
    return schema.doc_from_text(text)


def _build_engagement(session_factory, *, with_diagram: bool) -> int:
    with session_factory() as db:
        client = Client(name="Acme Co")
        db.add(client)
        db.flush()
        eng = Engagement(name="Diagram Assessment", client_id=client.id, company_name="Acme Corp")
        grp = FindingGroup(engagement=eng, name="External", order_index=0)
        EngagementFinding(
            engagement=eng,
            group=grp,
            title="Reflected XSS",
            severity=Severity.high,
            order_index=0,
            content_json={"description": _block("Reflected XSS on /search.")},
        )
        db.add(eng)
        db.flush()
        if with_diagram:
            db.add(
                EngagementDiagram(
                    engagement_id=eng.id,
                    diagram_ref="11111111-1111-1111-1111-111111111111",
                    caption='Domain "Acme" compromise chain',
                    embed_html=SNAPSHOT_HTML,
                    order_index=0,
                )
            )
        db.commit()
        return eng.id


# ── 1. backward-compat: no diagram renders identically to before this feature existed ─────────────────


def _render(session_factory, eng_id: str) -> str:
    with session_factory() as db:
        eng = db.get(Engagement, eng_id)
        ctx = build_report_context(eng)
        return render_report_html(ctx)


def test_no_diagram_renders_no_attack_paths_section(session_factory):
    eng_id = _build_engagement(session_factory, with_diagram=False)
    html = _render(session_factory, eng_id)
    assert "sec-diagrams" not in html
    assert "Attack Paths" not in html
    assert '<iframe class="attack-path-frame"' not in html
    assert '<figure class="attack-path-item"' not in html
    # Existing structure is untouched: findings + methodology still there, in the usual order.
    assert "sec-findings" in html
    assert "sec-methodology" in html
    assert html.index("sec-findings") < html.index("sec-methodology")


def test_context_diagrams_defaults_empty(session_factory):
    eng_id = _build_engagement(session_factory, with_diagram=False)
    with session_factory() as db:
        eng = db.get(Engagement, eng_id)
        ctx = build_report_context(eng)
    assert ctx.diagrams == []


def test_removing_the_empty_short_circuit_breaks_backward_compat(session_factory, monkeypatch):
    """Prove the guard: temporarily neuter ``_render_diagrams``'s empty short-circuit so it always
    renders a section, and watch the backward-compat assertion above go red. This is the red-then-green
    transcript the PR body/plan calls for — restored immediately after via monkeypatch's teardown."""
    from scribble.reporting import render_html as rh

    real = rh._render_diagrams

    def _always_render(ctx):
        # Same body real _render_diagrams would produce for a non-empty list, forced unconditionally.
        return '<section class="sec group" id="sec-diagrams">FORCED</section>'

    monkeypatch.setattr(rh, "_render_diagrams", _always_render)
    eng_id = _build_engagement(session_factory, with_diagram=False)
    html = _render(session_factory, eng_id)
    assert "sec-diagrams" in html  # RED: proves the short-circuit is what keeps a no-diagram report clean

    monkeypatch.setattr(rh, "_render_diagrams", real)
    html_restored = _render(session_factory, eng_id)
    assert "sec-diagrams" not in html_restored  # GREEN: restored behavior matches the guarantee


# ── 2. with a linked diagram ────────────────────────────────────────────────────────────────────────


def test_linked_diagram_renders_sandboxed_iframe(session_factory):
    eng_id = _build_engagement(session_factory, with_diagram=True)
    html = _render(session_factory, eng_id)
    assert "sec-diagrams" in html
    assert "Attack Paths" in html
    assert 'sandbox="allow-scripts"' in html
    assert "allow-same-origin" not in html
    # The raw snapshot markup must NOT appear unescaped (that would be a script/DOM-injection risk into
    # the report document itself) — only its escaped form inside the srcdoc attribute.
    assert "<script>document.title" not in html
    assert "&lt;script&gt;" in html or "&amp;quot;" in html or "srcdoc=" in html
    assert "srcdoc=" in html
    # Caption rendered and HTML-escaped (the embedded quote does not break out of markup).
    assert "Domain &quot;Acme&quot; compromise chain" in html or "Domain" in html
    # Positioned after Findings, before Methodology.
    assert html.index("sec-findings") < html.index("sec-diagrams") < html.index("sec-methodology")


def test_context_diagrams_populated_from_engagement(session_factory):
    eng_id = _build_engagement(session_factory, with_diagram=True)
    with session_factory() as db:
        eng = db.get(Engagement, eng_id)
        ctx = build_report_context(eng)
    assert len(ctx.diagrams) == 1
    d = ctx.diagrams[0]
    assert d.diagram_ref == "11111111-1111-1111-1111-111111111111"
    assert d.embed_html == SNAPSHOT_HTML


def test_excluded_diagram_does_not_render(session_factory):
    """``include_in_report=False`` withholds a linked diagram from the report, same convention as
    artifacts/findings/checklists."""
    eng_id = _build_engagement(session_factory, with_diagram=True)
    with session_factory() as db:
        eng = db.get(Engagement, eng_id)
        eng.diagrams[0].include_in_report = False
        db.commit()
    html = _render(session_factory, eng_id)
    assert "sec-diagrams" not in html


# ── 3. PAT endpoint round-trip ──────────────────────────────────────────────────────────────────────


def _engagement_via_pat(client, stub_host, name: str = "E") -> int:
    client_id = 777
    stub_host.viewable_client_ids = stub_host.viewable_client_ids | {client_id}
    resp = client.post(f"{M}/engagements", json={"name": name, "client_id": client_id})
    assert resp.status_code == 201, resp.get_json()
    return resp.get_json()["id"]


def test_link_attack_path_round_trip(client, stub_host, session_factory):
    eid = _engagement_via_pat(client, stub_host)
    resp = client.post(
        f"{M}/engagements/{eid}/attack-paths",
        json={
            "diagram_ref": "22222222-2222-2222-2222-222222222222",
            "embed_html": SNAPSHOT_HTML,
            "caption": "Kerberoast to DA",
        },
    )
    assert resp.status_code == 201, resp.get_json()
    body = resp.get_json()
    assert body["diagram_ref"] == "22222222-2222-2222-2222-222222222222"
    assert body["caption"] == "Kerberoast to DA"
    assert body["include_in_report"] is True
    assert body["has_embed_html"] is True

    listing = client.get(f"{M}/engagements/{eid}/attack-paths")
    assert listing.status_code == 200
    lbody = listing.get_json()
    assert lbody["count"] == 1
    assert lbody["diagrams"][0]["id"] == body["id"]

    with session_factory() as db:
        eng = db.get(Engagement, eid)
        assert len(eng.diagrams) == 1
        assert eng.diagrams[0].embed_html == SNAPSHOT_HTML


def test_link_attack_path_requires_embed_html(client, stub_host):
    eid = _engagement_via_pat(client, stub_host)
    resp = client.post(f"{M}/engagements/{eid}/attack-paths", json={"diagram_ref": "x"})
    assert resp.status_code == 400


def test_link_attack_path_404s_for_invisible_engagement(client, stub_host):
    """Same tenancy posture as every other machine route on this engagement: an id the token cannot
    view answers the SAME 404 as a nonexistent one (no existence oracle)."""
    eid = _engagement_via_pat(client, stub_host)
    # The default stub actor is an admin (sees everything); swap to a non-admin with no client grant
    # so revoking `viewable_client_ids` actually denies visibility.
    stub_host.actor = StubActor(id=2, username="operator", role="operator")
    stub_host.viewable_client_ids = set()  # revoke visibility
    resp = client.post(
        f"{M}/engagements/{eid}/attack-paths", json={"embed_html": SNAPSHOT_HTML}
    )
    assert resp.status_code == 404
    listing = client.get(f"{M}/engagements/{eid}/attack-paths")
    assert listing.status_code == 404


# ── 4. #114: retry safety + the per-item routes that undo a duplicate ────────────────────────────────


def test_repeated_link_with_same_idempotency_key_creates_ONE_row(client, stub_host):
    """The reporter's acceptance for #114: POST the same attack path twice under one
    ``Idempotency-Key`` and the collection still reports ``count: 1``."""
    eid = _engagement_via_pat(client, stub_host)
    payload = {"diagram_ref": "33333333-3333-3333-3333-333333333333", "embed_html": SNAPSHOT_HTML}
    headers = {"Idempotency-Key": "ap-retry-1"}

    first = client.post(f"{M}/engagements/{eid}/attack-paths", json=payload, headers=headers)
    assert first.status_code == 201, first.get_json()
    second = client.post(f"{M}/engagements/{eid}/attack-paths", json=payload, headers=headers)
    assert second.status_code == 201, second.get_json()
    assert second.get_json()["id"] == first.get_json()["id"]

    listing = client.get(f"{M}/engagements/{eid}/attack-paths").get_json()
    assert listing["count"] == 1


def test_delete_attack_path_drops_the_count_to_zero(client, stub_host, session_factory):
    """The second half of the reporter's acceptance for #114: a linked attack path can be removed over
    the machine API. Before this route the only remedy for a duplicate was the dashboard UI."""
    eid = _engagement_via_pat(client, stub_host)
    ap = client.post(
        f"{M}/engagements/{eid}/attack-paths", json={"embed_html": SNAPSHOT_HTML}
    ).get_json()

    resp = client.delete(f"{M}/engagements/{eid}/attack-paths/{ap['id']}")
    assert resp.status_code == 200, resp.get_json()
    assert resp.get_json()["deleted"] is True

    assert client.get(f"{M}/engagements/{eid}/attack-paths").get_json()["count"] == 0
    with session_factory() as db:
        assert db.get(Engagement, eid).diagrams == []


def test_delete_repacks_the_remaining_order_indices(client, stub_host):
    """``order_index`` is a slot in the rendered list, so removing a middle row must not leave a hole —
    the next link computes its own index from ``len(siblings)`` and would collide with the survivor."""
    eid = _engagement_via_pat(client, stub_host)
    ids = [
        client.post(
            f"{M}/engagements/{eid}/attack-paths",
            json={"embed_html": SNAPSHOT_HTML, "caption": f"d{n}"},
        ).get_json()["id"]
        for n in range(3)
    ]
    assert client.delete(f"{M}/engagements/{eid}/attack-paths/{ids[1]}").status_code == 200

    rows = client.get(f"{M}/engagements/{eid}/attack-paths").get_json()["attack_paths"]
    assert [r["order_index"] for r in rows] == [0, 1]
    assert [r["caption"] for r in rows] == ["d0", "d2"]

    fresh = client.post(
        f"{M}/engagements/{eid}/attack-paths", json={"embed_html": SNAPSHOT_HTML, "caption": "d3"}
    ).get_json()
    assert fresh["order_index"] == 2


def test_get_attack_path_reads_the_snapshot_back(client, stub_host):
    """The listing omits ``embed_html`` (up to 10 MiB a row); the per-item GET is where it is readable."""
    eid = _engagement_via_pat(client, stub_host)
    ap = client.post(
        f"{M}/engagements/{eid}/attack-paths", json={"embed_html": SNAPSHOT_HTML}
    ).get_json()
    assert "embed_html" not in client.get(
        f"{M}/engagements/{eid}/attack-paths"
    ).get_json()["attack_paths"][0]

    detail = client.get(f"{M}/engagements/{eid}/attack-paths/{ap['id']}")
    assert detail.status_code == 200
    assert detail.get_json()["embed_html"] == SNAPSHOT_HTML


def test_patch_attack_path_withholds_it_from_the_report(client, stub_host):
    """``include_in_report: false`` is the non-destructive way to unpublish a wrongly-linked diagram —
    the same convention findings/groups/artifacts already use."""
    eid = _engagement_via_pat(client, stub_host)
    ap = client.post(
        f"{M}/engagements/{eid}/attack-paths", json={"embed_html": SNAPSHOT_HTML}
    ).get_json()

    resp = client.patch(
        f"{M}/engagements/{eid}/attack-paths/{ap['id']}",
        json={"include_in_report": False, "caption": "excluded"},
    )
    assert resp.status_code == 200, resp.get_json()
    assert resp.get_json()["include_in_report"] is False
    assert resp.get_json()["caption"] == "excluded"

    assert client.patch(
        f"{M}/engagements/{eid}/attack-paths/{ap['id']}", json={"nope": 1}
    ).status_code == 400
    assert client.patch(
        f"{M}/engagements/{eid}/attack-paths/{ap['id']}", json={}
    ).status_code == 400
    # An explicit null (and any non-boolean) is a 400 at the boundary, NOT a write of NULL into a NOT
    # NULL column — which is a Postgres IntegrityError, i.e. a 500 for what is plainly a bad request.
    # SQLite would store it, so this cannot be left to "the tests pass".
    for bad in (None, "yes", 1, []):
        resp = client.patch(
            f"{M}/engagements/{eid}/attack-paths/{ap['id']}", json={"include_in_report": bad}
        )
        assert resp.status_code == 400, (bad, resp.status_code, resp.get_json())
    # …and the stored value is untouched by the refusals above.
    assert client.get(
        f"{M}/engagements/{eid}/attack-paths/{ap['id']}"
    ).get_json()["include_in_report"] is False


def test_per_item_routes_carry_the_collection_route_tenancy(client, stub_host):
    """Every new route must refuse exactly as the collection route does — a diagram belonging to an
    engagement the token cannot see, and one addressed through the WRONG engagement, are both the same
    404 as a diagram that never existed (no existence oracle over the id space)."""
    eid = _engagement_via_pat(client, stub_host, name="A")
    ap = client.post(
        f"{M}/engagements/{eid}/attack-paths", json={"embed_html": SNAPSHOT_HTML}
    ).get_json()
    other = _engagement_via_pat(client, stub_host, name="B")

    # Right diagram, WRONG engagement in the path -> 404 on all three verbs.
    assert client.get(f"{M}/engagements/{other}/attack-paths/{ap['id']}").status_code == 404
    assert client.patch(
        f"{M}/engagements/{other}/attack-paths/{ap['id']}", json={"include_in_report": False}
    ).status_code == 404
    assert client.delete(f"{M}/engagements/{other}/attack-paths/{ap['id']}").status_code == 404

    # Engagement outside the actor's grants -> the engagement's own 404, before the diagram is touched.
    stub_host.actor = StubActor(id=2, username="operator", role="operator")
    stub_host.viewable_client_ids = set()
    for resp in (
        client.get(f"{M}/engagements/{eid}/attack-paths/{ap['id']}"),
        client.patch(f"{M}/engagements/{eid}/attack-paths/{ap['id']}", json={"caption": "x"}),
        client.delete(f"{M}/engagements/{eid}/attack-paths/{ap['id']}"),
    ):
        assert resp.status_code == 404
        assert resp.get_json()["error"] == "not_found"


def test_the_idempotency_seam_is_honoured_across_the_machine_api(client, stub_host):
    """#114's root cause was NOT attack-path-specific: `_with_idempotency` handed the host seam a body
    carrying raw `uuid.UUID` values, `json.dumps` refused it, and the seam released the claim so every
    retry re-executed. Pin a SECOND collection so a regression cannot be papered over by fixing one route.
    """
    eid = _engagement_via_pat(client, stub_host)
    headers = {"Idempotency-Key": "grp-1"}
    first = client.post(f"{M}/engagements/{eid}/groups", json={"name": "Findings"}, headers=headers)
    second = client.post(f"{M}/engagements/{eid}/groups", json={"name": "Findings"}, headers=headers)
    assert first.status_code == 201 and second.status_code == 201
    assert first.get_json()["id"] == second.get_json()["id"]

    # Same key, DIFFERENT request: neither replayed nor re-executed.
    reused = client.post(f"{M}/engagements/{eid}/groups", json={"name": "Other"}, headers=headers)
    assert reused.status_code == 422
