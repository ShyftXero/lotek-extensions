"""Machine (PAT/Bearer) API — evidence/screenshot upload —
``POST /scribble/machine/engagements/<id>/artifacts`` (``scribble/api_pat.py``).

This is the route that makes "an agent writes the pentest report" complete: a tool on a token can attach
the screenshot or capture that backs a finding. Auth/scope RBAC is the HOST's concern (proven against a
real lotek host in the lotek repo); what is proven HERE is scribble's own logic — tenancy checked BEFORE
any byte is written, the size cap on both the pre- and post-decode paths, idempotent retries, and the
content-kind inference.

Also pins the ``__lotek_scope__`` stamp that ``host.require_scope`` now applies: without it the host's
OpenAPI generator does not recognise ANY of scribble's machine routes as PAT endpoints, so they are absent
from the generated spec — an agent cannot discover what it is allowed to call.
"""

from __future__ import annotations

import base64
import io

import pytest

import scribble.models as fm
from scribble import api_pat
from scribble.host import SCOPE_ATTR
from tests.conftest import StubActor

M = "/scribble/machine"

ACME = 501  # the client every machine-created engagement in this file belongs to

# A 1x1 PNG — real bytes, so content-type sniffing and kind inference are exercised for real.
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8AAAwAB/AL+2wAAAABJRU5ErkJggg=="
)


def _engagement(client, stub_host, name: str = "E") -> int:
    """Create the engagement under test — under a client THIS TOKEN can see (see
    tests/test_machine_engagements.py for why a machine engagement must name a visible client)."""
    stub_host.viewable_client_ids = stub_host.viewable_client_ids | {ACME}
    resp = client.post(f"{M}/engagements", json={"name": name, "client_id": ACME})
    assert resp.status_code == 201, resp.get_json()
    return resp.get_json()["id"]


def _upload_json(client, engagement_id, *, data=PNG, filename="shot.png", **extra):
    body = {"filename": filename, "content_base64": base64.b64encode(data).decode()}
    body.update(extra)
    return client.post(f"{M}/engagements/{engagement_id}/artifacts", json=body)


# ── the OpenAPI stamp (why host.py changed) ───────────────────────────────────────────────────────


def test_every_machine_route_carries_the_scope_stamp(app):
    """``require_scope`` stamps ``__lotek_scope__``, and that stamp IS how the host's generator decides a
    route is a PAT machine endpoint. Walks the live ``url_map`` so a route added later without the
    decorator fails here instead of quietly vanishing from the spec."""
    rules = [r for r in app.url_map.iter_rules() if str(r.rule).startswith(M)]
    assert rules, "no /scribble/machine routes are registered at all"
    unstamped = [str(r.rule) for r in rules if not hasattr(app.view_functions[r.endpoint], SCOPE_ATTR)]
    assert unstamped == [], f"machine routes missing require_scope: {unstamped}"


def test_the_upload_route_is_write_scoped(app):
    view = app.view_functions["scribble_machine.scribble_upload_artifact"]
    assert getattr(view, SCOPE_ATTR) == "write"


# ── the happy paths ───────────────────────────────────────────────────────────────────────────────


def test_upload_base64_attaches_evidence(client, stub_host, session_factory):
    eid = _engagement(client, stub_host)
    resp = _upload_json(client, eid, caption="Reflected XSS on /search")
    assert resp.status_code == 201, resp.get_json()
    body = resp.get_json()
    assert body["filename"] == "shot.png"
    assert body["kind"] == "screenshot"          # inferred from the sniffed image/* content type
    assert body["url"]

    with session_factory() as db:
        art = db.get(fm.Artifact, body["id"])
        assert art.engagement_id == eid
        assert art.byte_size == len(PNG)
        assert art.sha256
        assert art.caption == "Reflected XSS on /search"
        assert art.include_in_report is True
        # Attribution is the PAT principal's, not a session's — `current_actor` is None on this surface.
        assert art.created_by == stub_host.actor.username


def test_upload_multipart_attaches_evidence(client, stub_host, session_factory):
    """The other accepted shape: a real multipart file upload."""
    eid = _engagement(client, stub_host)
    resp = client.post(
        f"{M}/engagements/{eid}/artifacts",
        data={"file": (io.BytesIO(PNG), "capture.png"), "caption": "proof"},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 201, resp.get_json()
    with session_factory() as db:
        art = db.get(fm.Artifact, resp.get_json()["id"])
        assert art.filename == "capture.png"
        assert art.byte_size == len(PNG)


def test_a_text_artifact_is_classified_as_text(client, stub_host):
    eid = _engagement(client, stub_host)
    resp = _upload_json(client, eid, data=b"nmap output\n", filename="scan.txt")
    assert resp.status_code == 201
    assert resp.get_json()["kind"] == "text"


def test_upload_can_be_attached_to_a_finding(client, stub_host, session_factory):
    """Evidence usually belongs to one finding, which is how it lands in the right report section."""
    eid = _engagement(client, stub_host)
    with session_factory() as db:
        finding = fm.EngagementFinding(engagement_id=eid, title="XSS", severity="high")
        db.add(finding)
        db.commit()
        fid = finding.id
    resp = _upload_json(client, eid, finding_id=fid)
    assert resp.status_code == 201
    with session_factory() as db:
        assert db.get(fm.Artifact, resp.get_json()["id"]).finding_id == fid


# ── the response says WHERE the evidence actually landed (ext#40) ─────────────────────────────────


def _finding_on(session_factory, engagement_id: int, title: str = "XSS") -> int:
    with session_factory() as db:
        finding = fm.EngagementFinding(engagement_id=engagement_id, title=title, severity="high")
        db.add(finding)
        db.commit()
        return finding.id


def test_the_201_echoes_the_effective_finding_id(client, stub_host, session_factory):
    eid = _engagement(client, stub_host)
    fid = _finding_on(session_factory, eid)
    body = _upload_json(client, eid, finding_id=fid).get_json()
    assert body["finding_id"] == fid
    assert body["finding_id_dropped"] is False


def test_an_engagement_level_upload_reports_a_null_finding_id(client, stub_host):
    """Evidence attached to the engagement itself (no ``finding_id``) is legitimate — it renders in the
    report's Evidence appendix (ext#40) — and the response says so explicitly rather than leaving the
    caller to assume a finding attach happened."""
    eid = _engagement(client, stub_host)
    body = _upload_json(client, eid).get_json()
    assert body["finding_id"] is None
    assert body["finding_id_dropped"] is False


def test_a_foreign_finding_id_is_dropped_AND_the_caller_is_told(client, stub_host, session_factory):
    """The tenancy rule stays exactly as it was — a ``finding_id`` belonging to ANOTHER engagement is
    dropped rather than 404'd, so an attacker-chosen image can never be bolted onto someone else's
    finding — but the caller can now DETECT it. Before, both cases answered 201 with a URL and were
    indistinguishable, and the dropped one silently became engagement-level evidence."""
    eid = _engagement(client, stub_host)
    other = _engagement(client, stub_host, name="Other engagement")
    foreign_fid = _finding_on(session_factory, other, title="Someone else's finding")

    resp = _upload_json(client, eid, finding_id=foreign_fid)
    assert resp.status_code == 201, resp.get_json()
    body = resp.get_json()
    assert body["finding_id"] is None, "a foreign finding_id must not be honoured"
    assert body["finding_id_dropped"] is True, "a dropped attachment must be visible to the caller"
    with session_factory() as db:
        assert db.get(fm.Artifact, body["id"]).finding_id is None
        assert db.get(fm.EngagementFinding, foreign_fid).artifacts == []


def test_a_missing_finding_id_is_dropped_the_same_way(client, stub_host):
    eid = _engagement(client, stub_host)
    body = _upload_json(client, eid, finding_id=999999).get_json()
    assert body["finding_id"] is None
    assert body["finding_id_dropped"] is True


@pytest.mark.parametrize(
    "bad",
    [
        "0198f3c1-6a1e-7c0b-9a3e-2f5c8d7b4a11",  # a core UUIDv7 — the likeliest wrong value here
        "abc",
        "12.5",
        [],
    ],
)
def test_a_finding_id_that_does_not_PARSE_is_refused_not_silently_dropped(client, stub_host, bad):
    """``_as_int`` returns None for anything non-integer, which used to make an unparseable ``finding_id``
    indistinguishable from not sending one: the artifact landed as engagement-level evidence and the 201
    said ``finding_id_dropped: false`` — "you did not ask for one" — which is the exact false reassurance
    the echo fields were added to remove.

    A UUID is the specific mistake to expect: scribble's finding ids are sequential integers while the
    host's are UUIDv7, and mixing the two has cost this project real outages. Refusing it also keeps
    ``finding_id_dropped`` honest: after this, ``finding_id: null`` with ``dropped: false`` really does
    mean the caller asked for engagement-level. (A well-formed id belonging to ANOTHER engagement is still
    dropped rather than refused — that case would leak whether the id exists; this one cannot.)"""
    eid = _engagement(client, stub_host)
    resp = _upload_json(client, eid, finding_id=bad)
    assert resp.status_code == 400, resp.get_json()
    assert resp.get_json()["detail"] == "invalid finding_id"


def test_an_empty_finding_id_still_means_engagement_level(client, stub_host):
    """A form/JSON field left blank is "I am not attaching this to a finding", not a malformed id — the
    multipart surface in particular submits ``finding_id=""`` for an untouched field."""
    eid = _engagement(client, stub_host)
    body = _upload_json(client, eid, finding_id="").get_json()
    assert body["finding_id"] is None
    assert body["finding_id_dropped"] is False


def test_a_multipart_upload_refuses_an_unparseable_finding_id(client, stub_host):
    """Same rule on the multipart surface, where every field arrives as a string — so the JSON branch's
    check being present says nothing about this one."""
    eid = _engagement(client, stub_host)
    resp = client.post(
        f"{M}/engagements/{eid}/artifacts",
        data={
            "file": (io.BytesIO(PNG), "shot.png"),
            "finding_id": "0198f3c1-6a1e-7c0b-9a3e-2f5c8d7b4a11",
        },
        content_type="multipart/form-data",
    )
    assert resp.status_code == 400, resp.get_json()
    assert resp.get_json()["detail"] == "invalid finding_id"


def test_an_unparseable_finding_id_is_refused_on_a_REPLAY_too(client, stub_host):
    """The idempotent-replay branch reads the same parsed value, so an unparseable id there would answer
    200 with ``finding_id_dropped: false`` about a request that asked for something meaningless."""
    eid = _engagement(client, stub_host)
    assert _upload_json(client, eid, idempotency_key="k9").status_code == 201
    resp = _upload_json(client, eid, idempotency_key="k9", finding_id="not-an-int")
    assert resp.status_code == 400, resp.get_json()
    assert resp.get_json()["detail"] == "invalid finding_id"


def test_an_idempotent_replay_echoes_the_STORED_attachment(client, stub_host, session_factory):
    """A retry must report where the evidence actually sits, not what this request asked for: a replay
    whose ``finding_id`` differs from the stored one is told the attachment was not applied."""
    eid = _engagement(client, stub_host)
    fid = _finding_on(session_factory, eid)
    first = _upload_json(client, eid, idempotency_key="k1")  # engagement-level
    assert first.get_json()["finding_id"] is None

    replay = _upload_json(client, eid, idempotency_key="k1", finding_id=fid)
    assert replay.status_code == 200
    assert replay.get_json()["id"] == first.get_json()["id"]
    assert replay.get_json()["finding_id"] is None
    assert replay.get_json()["finding_id_dropped"] is True


def test_explicit_kind_and_placement_are_honoured(client, stub_host, session_factory):
    eid = _engagement(client, stub_host)
    resp = _upload_json(client, eid, kind="file", placement="inline")
    assert resp.status_code == 201
    with session_factory() as db:
        art = db.get(fm.Artifact, resp.get_json()["id"])
        assert art.kind.value == "file"
        assert art.placement.value == "inline"


# ── tenancy: checked BEFORE anything is written ───────────────────────────────────────────────────


def test_upload_to_an_invisible_engagement_is_404_and_writes_nothing(client, stub_host, session_factory):
    """The load-bearing check. The engagement comes from the URL, and the SAME predicate the rest of the
    module uses (`can_view_engagement` against the PAT actor) runs before a single byte is stored."""
    eid = _engagement(client, stub_host)
    # A non-admin token that holds no grant on this engagement's client.
    stub_host.actor = StubActor(id=2, username="other", role="operator")
    stub_host.viewable_client_ids = set()

    resp = _upload_json(client, eid)
    assert resp.status_code == 404
    with session_factory() as db:
        assert db.query(fm.Artifact).count() == 0, "a refused upload must not have written an artifact"


def test_upload_to_a_missing_engagement_is_the_same_404(client, stub_host):
    """Missing and not-visible are indistinguishable — no existence oracle."""
    _engagement(client, stub_host)  # so the table is not simply empty
    missing = client.post(f"{M}/engagements/999999/artifacts",
                          json={"filename": "x.png", "content_base64": base64.b64encode(PNG).decode()})
    assert missing.status_code == 404

    eid = _engagement(client, stub_host, name="E2")
    stub_host.actor = StubActor(id=2, username="other", role="operator")
    stub_host.viewable_client_ids = set()
    invisible = _upload_json(client, eid)
    assert invisible.status_code == 404
    assert invisible.get_json() == missing.get_json(), "the two must be byte-identical responses"


# ── the size cap, on BOTH paths ───────────────────────────────────────────────────────────────────


def test_oversized_decoded_payload_is_413(client, stub_host, monkeypatch, session_factory):
    """The authoritative post-decode cap."""
    monkeypatch.setattr(api_pat, "_MAX_ARTIFACT_BYTES", 1024)
    eid = _engagement(client, stub_host)
    resp = _upload_json(client, eid, data=b"A" * 2048)
    assert resp.status_code == 413
    assert resp.get_json()["error"] == "payload_too_large"
    with session_factory() as db:
        assert db.query(fm.Artifact).count() == 0


def test_an_oversized_base64_string_is_rejected_BEFORE_it_is_decoded(client, stub_host, monkeypatch):
    """The preflight check (the open review comment on lotek PR #288): a huge base64 string must be
    refused on its LENGTH, without being materialized through `b64decode` first.

    Proven by sending a string that is both oversized AND invalid base64. Preflight-first yields 413;
    decode-first would yield 400 for the invalid content. The status code is the discriminator.
    """
    monkeypatch.setattr(api_pat, "_MAX_ARTIFACT_BYTES", 1024)
    eid = _engagement(client, stub_host)
    oversized_and_invalid = "!" * 4096  # '!' is not in the base64 alphabet
    resp = client.post(f"{M}/engagements/{eid}/artifacts",
                       json={"filename": "big.bin", "content_base64": oversized_and_invalid})
    assert resp.status_code == 413, (
        f"expected the length preflight to reject before decoding, got {resp.status_code}: "
        f"{resp.get_json()}"
    )


def test_a_payload_at_the_cap_is_still_accepted(client, stub_host, monkeypatch):
    """The preflight bound must not be so tight that a legal upload is falsely rejected."""
    monkeypatch.setattr(api_pat, "_MAX_ARTIFACT_BYTES", 1024)
    eid = _engagement(client, stub_host)
    resp = _upload_json(client, eid, data=b"A" * 1024, filename="exact.bin")
    assert resp.status_code == 201, resp.get_json()


# ── idempotency ───────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("via_header", [False, True])
def test_a_retry_with_the_same_idempotency_key_returns_the_original(client, stub_host, session_factory,
                                                                   via_header):
    """A tool that retries a timed-out upload must not double-attach the same screenshot."""
    eid = _engagement(client, stub_host)
    key = "evidence-1"
    if via_header:
        payload = {"filename": "shot.png", "content_base64": base64.b64encode(PNG).decode()}
        first = client.post(f"{M}/engagements/{eid}/artifacts", json=payload,
                            headers={"Idempotency-Key": key})
        second = client.post(f"{M}/engagements/{eid}/artifacts", json=payload,
                             headers={"Idempotency-Key": key})
    else:
        first = _upload_json(client, eid, idempotency_key=key)
        second = _upload_json(client, eid, idempotency_key=key)

    assert first.status_code == 201, first.get_json()
    assert second.status_code == 200, second.get_json()
    assert second.get_json()["id"] == first.get_json()["id"]
    with session_factory() as db:
        assert db.query(fm.Artifact).count() == 1


def test_different_keys_produce_distinct_artifacts(client, stub_host, session_factory):
    eid = _engagement(client, stub_host)
    a = _upload_json(client, eid, idempotency_key="one")
    b = _upload_json(client, eid, idempotency_key="two")
    assert {a.status_code, b.status_code} == {201}
    assert a.get_json()["id"] != b.get_json()["id"]
    with session_factory() as db:
        assert db.query(fm.Artifact).count() == 2


# ── bad input ─────────────────────────────────────────────────────────────────────────────────────


def test_invalid_base64_is_400(client, stub_host):
    eid = _engagement(client, stub_host)
    resp = client.post(f"{M}/engagements/{eid}/artifacts",
                       json={"filename": "x.png", "content_base64": "not!valid!base64"})
    assert resp.status_code == 400


def test_a_non_string_content_field_is_400_not_a_crash(client, stub_host):
    eid = _engagement(client, stub_host)
    resp = client.post(f"{M}/engagements/{eid}/artifacts",
                       json={"filename": "x.png", "content_base64": {"nope": 1}})
    assert resp.status_code == 400


def test_missing_content_is_400(client, stub_host):
    eid = _engagement(client, stub_host)
    resp = client.post(f"{M}/engagements/{eid}/artifacts", json={"filename": "x.png"})
    assert resp.status_code == 400


def test_an_empty_payload_is_400(client, stub_host):
    eid = _engagement(client, stub_host)
    resp = _upload_json(client, eid, data=b"")
    assert resp.status_code == 400


def test_neither_multipart_nor_json_is_400(client, stub_host):
    eid = _engagement(client, stub_host)
    resp = client.post(f"{M}/engagements/{eid}/artifacts", data="raw", content_type="text/plain")
    assert resp.status_code == 400


def test_an_invalid_kind_or_placement_is_400(client, stub_host):
    eid = _engagement(client, stub_host)
    assert _upload_json(client, eid, kind="bogus").status_code == 400
    assert _upload_json(client, eid, placement="bogus").status_code == 400


def test_upload_fails_closed_with_no_host_mounted(client):
    """Standalone scribble has no PAT scheme, so the machine route must refuse rather than run
    unauthenticated (`scribble/host.py::_no_host`)."""
    resp = client.post(f"{M}/engagements/1/artifacts", json={"filename": "x", "content_base64": "eA=="})
    assert resp.status_code == 503
    assert resp.get_json()["error"] == "unavailable"
