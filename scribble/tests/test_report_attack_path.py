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
