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
import uuid

import pytest

import scribble.models as fm
from scribble import api_pat
from scribble.artifacts_storage import SAFE_NAME_MAX
from scribble.host import SCOPE_ATTR
from scribble.testing import read_evidence
from tests.conftest import StubActor

_MISSING_ID = uuid.uuid7()  # a well-formed id that is not in the table

M = "/scribble/machine"

# Scribble's own client PK is UUIDv7 since lotek#335. Where a test seeds `scribble_clients` and
# ALSO grants on the same id via the stub host, both halves must move together.
ACME = uuid.uuid7()  # the client every machine-created engagement in this file belongs to

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
    return uuid.UUID(resp.get_json()["id"])


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


def test_upload_rejects_overlong_filename(client, stub_host):
    """The machine route's own input-side cap (``_ARTIFACT_FILENAME_MAX_LEN``, == ``SAFE_NAME_MAX``)
    refuses a name over budget before a single byte is written."""
    eid = _engagement(client, stub_host)
    long_name = "a" * (SAFE_NAME_MAX + 1) + ".png"
    resp = _upload_json(client, eid, filename=long_name)
    assert resp.status_code == 400


def test_upload_bounds_stored_name_after_secure_filename_expansion(client, stub_host, session_factory, app):
    """#55 residual 1: a filename that EXPANDS under ``secure_filename`` must not break the upload.

    200 '½' characters sit well under the ``SAFE_NAME_MAX`` (222) input cap and expand to 400 ASCII,
    which used to overrun ``NAME_MAX`` (255) once the on-disk writer prefixed a uuid — an ENAMETOOLONG
    500 on a legitimate upload. Evidence is keyed by object id now, so no filesystem name is derived
    from the caller's at all and that whole failure mode is structurally gone. What still matters, and
    is what this asserts, is that the row keeps the caller's filename and the bytes come back."""
    eid = _engagement(client, stub_host)
    filename = "½" * 200 + ".png"
    resp = _upload_json(client, eid, filename=filename)
    assert resp.status_code == 201, resp.get_json()

    with session_factory() as db:
        art = db.get(fm.Artifact, resp.get_json()["id"])
        assert art.filename == filename, "the caller's own filename is preserved on the row"
        assert read_evidence(app, art.storage_path) is not None


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
    assert body["finding_id"] == str(fid)
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


def test_a_nonexistent_finding_id_is_dropped_the_same_way(client, stub_host):
    """A WELL-FORMED id with no row anywhere gets the same silent drop as a foreign one -- refusing it
    would leak whether the id exists (see ``_finding_id_or_400``'s docstring)."""
    eid = _engagement(client, stub_host)
    body = _upload_json(client, eid, finding_id=_MISSING_ID).get_json()
    assert body["finding_id"] is None
    assert body["finding_id_dropped"] is True


@pytest.mark.parametrize(
    "bad",
    [
        "abc",
        "12.5",
        [],
        {},
        # None of these is a UUID, and `_as_uuid` rejects every one of them outright (it never coerces --
        # see its docstring) -- but a caller-supplied `finding_id` that fails to parse must still be
        # REFUSED, not silently treated as "no finding_id" (adversarial review, 2026-08-17, against the
        # old int-keyed id: `int()` coerced `2.9` -> finding 2 and `True` -> finding 1, a DIFFERENT
        # finding than the caller named, with the 201 asserting `finding_id_dropped: false`). Kept here as
        # the same class of malformed input against the current UUID parser.
        2.9,
        0.0,
        True,
        False,
        10**30,
        2**31,
        -1,
        0,
        "1e3",
        "+7",
        "\u0667",  # Arabic-Indic seven
        "\uff17",  # fullwidth seven
    ],
)
def test_a_finding_id_that_does_not_PARSE_is_refused_not_silently_dropped(client, stub_host, bad):
    """An unparseable ``finding_id`` used to be indistinguishable from not sending one: the artifact
    landed as engagement-level evidence and the 201 said ``finding_id_dropped: false`` -- "you did not ask
    for one" -- which is the exact false reassurance the echo fields were added to remove.

    None of these values is a UUID -- scribble's finding ids became UUIDv7 in lotek#335 -- and
    ``_finding_id_or_400`` refuses every one of them with a clean 400 rather than silently dropping the
    attachment. (A WELL-FORMED id belonging to ANOTHER engagement, or to none at all, is still dropped
    rather than refused -- that case would leak whether the id exists; a malformed one cannot leak
    anything, so there is no reason to be quiet about it. See
    ``test_a_well_formed_but_foreign_shaped_finding_id_is_dropped_not_refused``.)"""
    eid = _engagement(client, stub_host)
    resp = _upload_json(client, eid, finding_id=bad)
    assert resp.status_code == 400, resp.get_json()
    assert resp.get_json()["detail"] == "invalid finding_id"


def test_a_well_formed_but_foreign_shaped_finding_id_is_dropped_not_refused(client, stub_host):
    """A syntactically valid UUID that just does not name a finding on THIS engagement -- e.g. a core
    (host) UUIDv7 rather than one of scribble's own -- is the "well-formed but wrong" case, and it is
    silently dropped exactly like a foreign or nonexistent finding, never refused: refusing a well-formed
    id would tell the caller whether that id exists somewhere, which is the existence oracle this route
    must not become."""
    eid = _engagement(client, stub_host)
    body = _upload_json(client, eid, finding_id="0198f3c1-6a1e-7c0b-9a3e-2f5c8d7b4a11").get_json()
    assert body["finding_id"] is None
    assert body["finding_id_dropped"] is True


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
        data={"file": (io.BytesIO(PNG), "shot.png"), "finding_id": "not-a-uuid"},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 400, resp.get_json()
    assert resp.get_json()["detail"] == "invalid finding_id"


def test_an_out_of_range_finding_id_is_refused_BEFORE_the_bytes_are_stored(
    client, stub_host, session_factory, tmp_path
):
    """A caller-supplied ``finding_id`` that cannot parse must be refused before any byte is written —
    not just before the row lands. ``save_bytes`` and the ``Artifact`` insert both come AFTER the parse,
    so a malformed id must never leave an orphan file on disk behind a refused request."""
    eid = _engagement(client, stub_host)
    resp = _upload_json(client, eid, finding_id=10**30)
    assert resp.status_code == 400, resp.get_json()
    assert resp.get_json()["detail"] == "invalid finding_id"
    with session_factory() as db:
        assert db.query(fm.Artifact).count() == 0
    stored = [pth for pth in (tmp_path / "artifacts").rglob("*") if pth.is_file()]
    assert stored == [], f"a refused upload left bytes on disk: {stored}"


def test_a_multipart_upload_refuses_a_non_uuid_finding_id(client, stub_host):
    """Same rule on the form surface, where every value arrives as a string."""
    eid = _engagement(client, stub_host)
    resp = client.post(
        f"{M}/engagements/{eid}/artifacts",
        data={"file": (io.BytesIO(PNG), "shot.png"), "finding_id": str(2**31)},
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
    missing = client.post(f"{M}/engagements/{_MISSING_ID}/artifacts",
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
    assert uuid.UUID(second.get_json()["id"]) == uuid.UUID(first.get_json()["id"])
    with session_factory() as db:
        assert db.query(fm.Artifact).count() == 1


def test_different_keys_produce_distinct_artifacts(client, stub_host, session_factory):
    eid = _engagement(client, stub_host)
    a = _upload_json(client, eid, idempotency_key="one")
    b = _upload_json(client, eid, idempotency_key="two")
    assert {a.status_code, b.status_code} == {201}
    assert a.get_json()["id"] != uuid.UUID(b.get_json()["id"])
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
    resp = client.post(
        f"{M}/engagements/{uuid.uuid7()}/artifacts",
        json={"filename": "x", "content_base64": "eA=="},
    )  # a well-formed id: the point is the 503 from an unmounted host, not a routing 404
    assert resp.status_code == 503
    assert resp.get_json()["error"] == "unavailable"


# ── the review surface: publish is a DECISION, and the set that ships is listable ──────────────────
#
# ext#40 changed what an engagement-level artifact MEANS. Before it, an upload with no ``finding_id``
# reached no deliverable at all, so "unattached" was in practice "not in the report"; after it, the report's
# Evidence appendix publishes it. That is the behaviour the issue asked for, but shipped on its own it made
# publication a side effect of an upgrade: every such row already on disk — working material an operator
# attached precisely BECAUSE nothing rendered it — becomes client-facing on the next render, and there was
# no route through which anyone could see the set first or take one back out (adversarial review,
# 2026-08-17). The three routes below are that surface: decide at upload, list what is going to ship, flip
# one afterwards. The DEFAULT is still to publish — see the plan file for why flipping it would restore
# ext#40's symptom for the very PAT workflow that filed it.


def test_the_upload_response_says_whether_the_evidence_will_SHIP(client, stub_host):
    """The engagement-level upload is the one whose answer changed meaning, so the response has to say so
    rather than leave the caller to infer it from a URL."""
    eid = _engagement(client, stub_host)
    body = _upload_json(client, eid).get_json()
    assert body["finding_id"] is None
    assert body["include_in_report"] is True


def test_include_in_report_false_attaches_the_evidence_WITHOUT_publishing_it(
    client, stub_host, session_factory
):
    """The decision an agent attaching working material needs: stored and addressable, absent from the
    deliverable. ``_artifact_ctxs`` honours the flag, so the appendix never sees it."""
    eid = _engagement(client, stub_host)
    body = _upload_json(client, eid, include_in_report=False).get_json()
    assert body["include_in_report"] is False
    with session_factory() as db:
        assert db.get(fm.Artifact, body["id"]).include_in_report is False


@pytest.mark.parametrize(("raw", "expected"), [("false", False), ("0", False), ("true", True), ("on", True)])
def test_the_multipart_surface_takes_the_flag_as_a_WORD(client, stub_host, raw, expected):
    """A form can only send text, and ``bool("false")`` is True — the wrong parse for a flag that decides
    whether something reaches a client. The word forms are parsed explicitly."""
    eid = _engagement(client, stub_host)
    resp = client.post(
        f"{M}/engagements/{eid}/artifacts",
        data={"file": (io.BytesIO(PNG), "shot.png"), "include_in_report": raw},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 201, resp.get_json()
    assert resp.get_json()["include_in_report"] is expected


def test_a_nonsense_include_in_report_is_refused_not_guessed_at(client, stub_host):
    eid = _engagement(client, stub_host)
    resp = _upload_json(client, eid, include_in_report="maybe")
    assert resp.status_code == 400, resp.get_json()
    assert resp.get_json()["detail"] == "invalid include_in_report"


def test_a_replay_echoes_the_STORED_publish_decision(client, stub_host):
    """Same rule as ``finding_id`` on the idempotent path: the reply describes the artifact that exists,
    not what this retry asked for."""
    eid = _engagement(client, stub_host)
    first = _upload_json(client, eid, idempotency_key="p1", include_in_report=False)
    assert first.get_json()["include_in_report"] is False
    replay = _upload_json(client, eid, idempotency_key="p1", include_in_report=True)
    assert replay.status_code == 200
    assert replay.get_json()["include_in_report"] is False


def test_the_list_route_shows_the_engagement_level_evidence_that_will_SHIP(
    client, stub_host, session_factory
):
    """The gap the appendix opened: the cookie API lists a FINDING's artifacts, which by construction
    cannot show a row whose ``finding_id`` is null, and there is no UI for them — so the rendered report
    was the only place an operator could discover what was about to be published."""
    eid = _engagement(client, stub_host)
    fid = _finding_on(session_factory, eid)
    attached = _upload_json(client, eid, finding_id=fid, filename="on-finding.png").get_json()["id"]
    loose = _upload_json(client, eid, filename="loose.pcap", data=b"\xd4\xc3\xb2\xa1raw").get_json()["id"]

    listing = client.get(f"{M}/engagements/{eid}/artifacts")
    assert listing.status_code == 200
    rows = {r["id"]: r for r in listing.get_json()["artifacts"]}
    assert set(rows) == {attached, loose}
    assert listing.get_json()["count"] == 2
    assert rows[loose]["finding_id"] is None
    assert rows[loose]["include_in_report"] is True   # ...which is exactly what needs reviewing
    assert rows[loose]["filename"] == "loose.pcap"
    assert rows[loose]["byte_size"] == 7

    unattached = client.get(f"{M}/engagements/{eid}/artifacts?unattached=1")
    assert [r["id"] for r in unattached.get_json()["artifacts"]] == [loose]


def test_the_list_route_is_scoped_to_the_engagement_in_the_URL(client, stub_host, session_factory):
    """Two engagements the same token can see: the listing must still be one engagement's."""
    eid = _engagement(client, stub_host)
    other = _engagement(client, stub_host, name="Other")
    mine = _upload_json(client, eid, filename="mine.png").get_json()["id"]
    _upload_json(client, other, filename="theirs.png")
    rows = client.get(f"{M}/engagements/{eid}/artifacts").get_json()["artifacts"]
    assert [r["id"] for r in rows] == [mine]


def test_listing_an_invisible_engagement_is_the_same_404_as_a_missing_one(client, stub_host):
    """No existence oracle, the same posture as every other route in this module."""
    eid = _engagement(client, stub_host)
    # A well-formed id with no row -- NOT a malformed one (lotek#335: a bare int no longer MATCHES the
    # `<uuid:...>` route at all, so Werkzeug's own 404 page would answer instead of the view's JSON 404).
    missing = client.get(f"{M}/engagements/{_MISSING_ID}/artifacts")
    stub_host.actor = StubActor(id=2, username="other", role="operator")
    stub_host.viewable_client_ids = set()
    invisible = client.get(f"{M}/engagements/{eid}/artifacts")
    assert missing.status_code == invisible.status_code == 404
    assert missing.get_json() == invisible.get_json()


def test_the_toggle_route_takes_an_artifact_back_OUT_of_the_deliverable(
    client, stub_host, session_factory
):
    """The other half, and PAT-reachable on purpose: the cookie route needs a session cookie and CSRF, so
    an agent that had just published working material had no way to undo it."""
    eid = _engagement(client, stub_host)
    aid = _upload_json(client, eid, filename="working.pcap").get_json()["id"]
    resp = client.post(f"{M}/engagements/{eid}/artifacts/{aid}", json={"include_in_report": False})
    assert resp.status_code == 200, resp.get_json()
    assert resp.get_json()["include_in_report"] is False
    with session_factory() as db:
        assert db.get(fm.Artifact, aid).include_in_report is False
    # ...and it is now visibly withheld on the review surface.
    rows = client.get(f"{M}/engagements/{eid}/artifacts").get_json()["artifacts"]
    assert rows[0]["include_in_report"] is False


def test_the_toggle_route_can_also_fix_a_caption(client, stub_host, session_factory):
    eid = _engagement(client, stub_host)
    aid = _upload_json(client, eid, caption="typo").get_json()["id"]
    resp = client.post(f"{M}/engagements/{eid}/artifacts/{aid}", json={"caption": "Scope diagram"})
    assert resp.get_json()["caption"] == "Scope diagram"
    assert resp.get_json()["include_in_report"] is True, "an omitted field must be left alone"
    with session_factory() as db:
        assert db.get(fm.Artifact, aid).caption == "Scope diagram"


def test_the_toggle_route_refuses_an_artifact_from_ANOTHER_engagement(
    client, stub_host, session_factory
):
    """The artifact is addressed THROUGH its engagement so authorization is the one predicate this module
    uses everywhere. A well-formed id belonging elsewhere is 404 even for a token that can see both, so the
    route cannot be used to reach into another engagement's evidence."""
    eid = _engagement(client, stub_host)
    other = _engagement(client, stub_host, name="Other")
    foreign = _upload_json(client, other, filename="theirs.png").get_json()["id"]
    resp = client.post(f"{M}/engagements/{eid}/artifacts/{foreign}", json={"include_in_report": False})
    assert resp.status_code == 404, resp.get_json()
    with session_factory() as db:
        assert db.get(fm.Artifact, foreign).include_in_report is True, "the foreign row must be untouched"


def test_the_toggle_route_refuses_an_invisible_engagement_before_reading_the_body(client, stub_host):
    """The tenancy gate runs FIRST, and the body is parsed only after it passes.

    The assertion that proves the ordering is the MALFORMED body: if the parse ran first it would answer
    ``400 invalid include_in_report`` and tell a caller with no grant on this engagement something about
    its own request that it should not get to learn here -- and, worse, the same 400 whether the engagement
    exists or not is the shape a reader mistakes for "the id is fine, fix your body". A well-formed body
    alone cannot detect the ordering at all, which is what this test used to assert."""
    eid = _engagement(client, stub_host)
    aid = _upload_json(client, eid).get_json()["id"]
    stub_host.actor = StubActor(id=2, username="other", role="operator")
    stub_host.viewable_client_ids = set()
    resp = client.post(f"{M}/engagements/{eid}/artifacts/{aid}", json={"include_in_report": False})
    assert resp.status_code == 404
    assert resp.get_json()["error"] == "not_found"

    bad = client.post(f"{M}/engagements/{eid}/artifacts/{aid}", json={"include_in_report": "sometimes"})
    assert bad.status_code == 404, (
        "a malformed body was parsed BEFORE the tenancy gate: "
        f"{bad.status_code} {bad.get_json()}"
    )
    assert bad.get_json()["error"] == "not_found"


def test_the_toggle_route_refuses_a_nonsense_flag(client, stub_host):
    eid = _engagement(client, stub_host)
    aid = _upload_json(client, eid).get_json()["id"]
    resp = client.post(f"{M}/engagements/{eid}/artifacts/{aid}", json={"include_in_report": "sometimes"})
    assert resp.status_code == 400
    assert resp.get_json()["detail"] == "invalid include_in_report"


def test_a_huge_id_in_the_PATH_is_a_routing_refusal_not_a_500(client, stub_host):
    """The same overflow as an out-of-range body ``finding_id``, reached through the URL instead.

    Werkzeug's bare ``<int:x>`` is unbounded, so a 30-digit path segment ROUTED and then raised inside
    ``db.get()``: measured 500 on all three of these before the converters were bounded (``DataError`` on
    Postgres, which also poisons the open transaction). Now the rule simply does not match, so the answer is
    a routing refusal — 404, or 405 where a same-path rule with another method exists — and no view runs.
    ``tests/test_scribble_machine_tenancy.py::test_every_machine_route_id_converter_is_BOUNDED`` is the
    drift guard that keeps this true for routes added later."""
    eid = _engagement(client, stub_host)
    huge = 10**30
    for resp in (
        client.get(f"{M}/engagements/{huge}"),
        client.get(f"{M}/engagements/{huge}/artifacts"),
        client.post(f"{M}/engagements/{eid}/artifacts/{huge}", json={"include_in_report": False}),
        _upload_json(client, huge),
    ):
        assert resp.status_code in (404, 405), resp.status_code


# ── audit trail (ext#63, INV-AUDIT-03) ───────────────────────────────────────────────────────────


def test_upload_emits_audit_row(client, stub_host):
    """Every other mutating route in this module calls ``_audit``; the upload route now does too, so
    who attached evidence to a deliverable is on the record."""
    eid = _engagement(client, stub_host)
    stub_host.audit_calls.clear()  # isolate the upload's own row from create_engagement's
    body = _upload_json(client, eid, caption="proof").get_json()
    assert len(stub_host.audit_calls) == 1
    action, kw = stub_host.audit_calls[0]
    assert action == "ext:scribble:upload_artifact"
    assert kw["subject_type"] == "artifact"
    # `subject_id` is the real `uuid.UUID` the audit seam is handed; `body["id"]` came back through JSON
    # and is its string form. Compared raw these are never equal — the assertion has been dead since the
    # UUIDv7 migration (#36 / lotek#335), when both sides stopped being ints. Same for `engagement_id`.
    assert str(kw["subject_id"]) == body["id"]
    assert kw["after"]["include_in_report"] is True
    assert str(kw["after"]["engagement_id"]) == str(eid)


def test_update_emits_audit_with_transition(client, stub_host):
    """The toggle route is the more sensitive one -- it can take evidence back OUT of a report that may
    already have been sent -- and its audit row must carry the include_in_report TRANSITION, not just the
    new value."""
    eid = _engagement(client, stub_host)
    aid = _upload_json(client, eid, caption="x").get_json()["id"]
    stub_host.audit_calls.clear()  # isolate the toggle's own row from the upload's

    resp = client.post(
        f"{M}/engagements/{eid}/artifacts/{aid}",
        json={"include_in_report": False, "caption": "y"},
    )
    assert resp.status_code == 200, resp.get_json()
    assert len(stub_host.audit_calls) == 1
    action, kw = stub_host.audit_calls[0]
    assert action == "ext:scribble:update_artifact"
    assert kw["subject_type"] == "artifact"
    assert str(kw["subject_id"]) == aid  # UUID vs its JSON string form — see the note above
    assert kw["before"]["include_in_report"] is True
    assert kw["after"]["include_in_report"] is False
    assert kw["before"]["caption"] == "x"
    assert kw["after"]["caption"] == "y"


def test_a_no_op_toggle_writes_no_audit_row(client, stub_host):
    """An empty/no-op body (no include_in_report, no caption key) changes nothing -- it must not write
    a before==after audit row that claims something happened when it didn't."""
    eid = _engagement(client, stub_host)
    aid = _upload_json(client, eid, caption="x").get_json()["id"]
    stub_host.audit_calls.clear()

    resp = client.post(f"{M}/engagements/{eid}/artifacts/{aid}", json={})
    assert resp.status_code == 200, resp.get_json()
    assert stub_host.audit_calls == []
