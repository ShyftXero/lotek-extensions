"""The scan-coverage gate on promotion (lotek#656) — `scribble/coverage.py` + `api_pat.py`.

The defect these pin is the 2026-09-04 AD-demo post-mortem: six jobs ran against a lab that was never
up and every one reported `completed / findings=0`, so a report was built from scans that never
examined anything. Core now distinguishes the three outcomes (`JobDTO.assessed`, lotek PR #927) and
deliberately declines to act on them; ACTING on them is this extension's job, at the one boundary
where scan output becomes a client deliverable.

What is pinned here:
  * all three verdicts — `True` promotes, `False` refuses, `None` refuses;
  * `None` refuses *exactly as hard as* `False`. That is the whole issue, not a detail: "we do not
    know" reaching a deliverable IS the original defect, so a test that only covered `False` would
    leave the incident reproducible;
  * a refusal is total — no findings, no host-side promotion record, nothing to clean up;
  * the override is deliberate: absent, empty, `false`, and unparseable all refuse;
  * an acknowledged promotion NAMES the unassessed modules in the engagement, and does so idempotently;
  * a host too old to answer at all fails CLOSED.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

import scribble.models as fm
from scribble.coverage import COVERAGE_NOTE_TITLE
from scribble.enums import Severity
from tests.conftest import FakeFindingDTO, StubActor

M = "/scribble/machine"

ACME = uuid.uuid7()

# The board's CORE engagement anchor (#845). Every board here is created against it and every job below
# is registered carrying it, because the promote anchor guard fails CLOSED when either side is missing:
# left unset, every promote in this file would 409 on TENANCY, and these tests would be asserting the
# anchor guard while appearing to assert the coverage one.
CORE = uuid.uuid7()

# The two modules that produced nothing in the post-mortem's pipeline shape.
UNASSESSED = ("nuclei", "enum4linux")


def _engagement(client, stub_host, name: str = "E") -> uuid.UUID:
    stub_host.viewable_client_ids = stub_host.viewable_client_ids | {ACME}
    resp = client.post(
        f"{M}/engagements", json={"name": name, "client_id": ACME, "core_engagement_id": str(CORE)}
    )
    assert resp.status_code == 201, resp.get_json()
    return uuid.UUID(resp.get_json()["id"])


def _operator(stub_host) -> None:
    stub_host.actor = StubActor(id=7, username="opA", role="operator")


def _findings(db, eid) -> list:
    return list(db.get(fm.ReportBoard, eid).findings)


def _notes(db, eid, job_id: str = "job-1") -> list:
    title = COVERAGE_NOTE_TITLE.format(job_id=job_id)
    return [f for f in _findings(db, eid) if f.title == title]


# ── the three verdicts ────────────────────────────────────────────────────────────────────────────


def test_assessed_job_promotes(client, stub_host, session_factory):
    """`assessed=True` — a module produced tool evidence. Normal promotion, nothing in the way."""
    stub_host.findings.add_job(
        "job-1",
        engagement_id=CORE, owner_id=7, dtos=[FakeFindingDTO(id=1, title="SQLi")], assessed=True
    )
    _operator(stub_host)
    eid = _engagement(client, stub_host)

    r = client.post(f"{M}/engagements/{eid}/promote-job/job-1")
    assert r.status_code == 200
    body = r.get_json()
    assert body["promoted"] == 1
    assert body["assessed"] is True
    assert body["coverage_acknowledged"] is False

    with session_factory() as db:
        assert {f.title for f in _findings(db, eid)} == {"SQLi"}  # the finding, and NO coverage note


@pytest.mark.parametrize(
    ("assessed", "detail_fragment"),
    [
        # Every module ran and measured nothing -- the scan happened and found nothing to assess.
        (False, "produced no tool evidence"),
        # Coverage was never measured. This is the verdict the incident actually had, and the one a
        # permissive reading ("we only know it's bad when it says False") would let straight through.
        (None, "never measured"),
    ],
    ids=["assessed_false", "assessed_none"],
)
def test_unassessed_job_is_refused(client, stub_host, session_factory, assessed, detail_fragment):
    stub_host.findings.add_job(
        "job-1",
        engagement_id=CORE,
        owner_id=7,
        dtos=[FakeFindingDTO(id=1, title="SQLi")],
        assessed=assessed,
        unassessed_modules=UNASSESSED,
    )
    _operator(stub_host)
    eid = _engagement(client, stub_host)

    r = client.post(f"{M}/engagements/{eid}/promote-job/job-1")
    assert r.status_code == 409
    body = r.get_json()
    assert body["error"] == "job_not_assessed"
    assert detail_fragment in body["detail"]
    assert body["assessed"] is assessed
    # The refusal NAMES the modules, so the honest next step (fix the scan) is as available as the
    # override -- a bare "refused" would push every operator straight to acknowledging it.
    assert body["unassessed_modules"] == list(UNASSESSED)

    # A refusal is TOTAL: no findings, no coverage note, and no promotion recorded on the host.
    with session_factory() as db:
        assert _findings(db, eid) == []
    assert stub_host.promoted_calls == []


def test_none_and_false_refuse_identically(client, stub_host):
    """`None` is not a softer `False`. Same status, same code — pinned against each other directly."""
    _operator(stub_host)
    eid = _engagement(client, stub_host)
    stub_host.findings.add_job(
        "j-false",
        engagement_id=CORE,
        owner_id=7,
        dtos=[FakeFindingDTO(id=1)],
        assessed=False,
    )
    stub_host.findings.add_job(
        "j-none",
        engagement_id=CORE,
        owner_id=7,
        dtos=[FakeFindingDTO(id=2)],
        assessed=None,
    )

    false_r = client.post(f"{M}/engagements/{eid}/promote-job/j-false")
    none_r = client.post(f"{M}/engagements/{eid}/promote-job/j-none")

    assert false_r.status_code == none_r.status_code == 409
    assert false_r.get_json()["error"] == none_r.get_json()["error"] == "job_not_assessed"


def test_host_without_the_coverage_field_fails_closed(client, stub_host, session_factory):
    """A host pinned to a core predating PR #927 has no `assessed` attribute at all.

    It knows even less than one answering `None`, so reading "absent" as "fine" would hand exactly
    those deployments the pre-#656 behaviour while this file stayed green.
    """
    from types import SimpleNamespace

    _operator(stub_host)
    eid = _engagement(client, stub_host)
    stub_host.findings.add_job(
        "job-1",
        engagement_id=CORE,
        owner_id=7,
        dtos=[FakeFindingDTO(id=1, title="SQLi")],
    )
    # An OLD host's job DTO: the coverage fields simply are not there.
    stub_host.findings.get_job = lambda job_id, actor: SimpleNamespace(  # noqa: ARG005
        id="job-1", promoted_extension=None, promoted_ref_id=None
    )

    r = client.post(f"{M}/engagements/{eid}/promote-job/job-1")
    assert r.status_code == 409
    assert r.get_json()["assessed"] is None
    with session_factory() as db:
        assert _findings(db, eid) == []


# ── the override ──────────────────────────────────────────────────────────────────────────────────


def test_acknowledged_promotion_writes_a_coverage_note_naming_the_modules(
    client, stub_host, session_factory
):
    stub_host.findings.add_job(
        "job-1",
        engagement_id=CORE,
        owner_id=7,
        dtos=[FakeFindingDTO(id=1, title="SQLi")],
        assessed=False,
        unassessed_modules=UNASSESSED,
    )
    _operator(stub_host)
    eid = _engagement(client, stub_host)

    r = client.post(
        f"{M}/engagements/{eid}/promote-job/job-1", json={"acknowledge_inconclusive": True}
    )
    assert r.status_code == 200
    body = r.get_json()
    assert body["promoted"] == 1
    assert body["coverage_acknowledged"] is True
    assert body["unassessed_modules"] == list(UNASSESSED)

    with session_factory() as db:
        notes = _notes(db, eid)
        assert len(notes) == 1
        note = notes[0]
        # info severity: a disclosure, not a vulnerability -- it must not inflate the risk rollup.
        assert note.severity == Severity.info
        prose = str(note.content_json)
        # The DELIVERABLE names the modules. This is the bullet the issue is most specific about: a
        # report that carried the findings but stayed silent about the gap is the defect wearing a
        # different hat.
        for module in UNASSESSED:
            assert module in prose
        assert "SQLi" in {f.title for f in _findings(db, eid)}


def test_acknowledged_promotion_of_an_unmeasured_job_says_so_without_inventing_modules(
    client, stub_host, session_factory
):
    """`assessed=None` carries NO per-module verdicts, so the note must not imply the list is complete.

    Rendering an empty list as "no modules failed" would invert the meaning into the exact reassurance
    the issue exists to prevent.
    """
    stub_host.findings.add_job(
        "job-1",
        engagement_id=CORE, owner_id=7, dtos=[FakeFindingDTO(id=1, title="SQLi")], assessed=None
    )
    _operator(stub_host)
    eid = _engagement(client, stub_host)

    r = client.post(
        f"{M}/engagements/{eid}/promote-job/job-1", json={"acknowledge_inconclusive": True}
    )
    assert r.status_code == 200

    with session_factory() as db:
        prose = str(_notes(db, eid)[0].content_json)
        assert "cannot be listed" in prose
        assert "unknown" in prose


def test_acknowledged_rerun_refreshes_one_note_rather_than_stacking(client, stub_host, session_factory):
    """`promote_job` is idempotent; the note must be too, or a retry looks like worsening coverage."""
    stub_host.findings.add_job(
        "job-1",
        engagement_id=CORE,
        owner_id=7,
        dtos=[FakeFindingDTO(id=1, title="SQLi")],
        assessed=False,
        unassessed_modules=UNASSESSED,
    )
    _operator(stub_host)
    eid = _engagement(client, stub_host)
    payload = {"acknowledge_inconclusive": True}

    client.post(f"{M}/engagements/{eid}/promote-job/job-1", json=payload)
    r2 = client.post(f"{M}/engagements/{eid}/promote-job/job-1", json=payload)
    assert r2.status_code == 200
    assert r2.get_json()["promoted"] == 0 and r2.get_json()["skipped"] == 1

    with session_factory() as db:
        assert len(_notes(db, eid)) == 1


def test_two_jobs_get_two_distinct_notes(client, stub_host, session_factory):
    """One engagement fed by two partially-assessed scans: a reader must be able to tell them apart."""
    _operator(stub_host)
    eid = _engagement(client, stub_host)
    stub_host.findings.add_job(
        "job-1",
        engagement_id=CORE, owner_id=7, dtos=[FakeFindingDTO(id=1, title="A")], assessed=False,
        unassessed_modules=("nuclei",),
    )
    stub_host.findings.add_job(
        "job-2",
        engagement_id=CORE, owner_id=7, dtos=[FakeFindingDTO(id=2, title="B")], assessed=False,
        unassessed_modules=("enum4linux",),
    )
    payload = {"acknowledge_inconclusive": True}
    client.post(f"{M}/engagements/{eid}/promote-job/job-1", json=payload)
    client.post(f"{M}/engagements/{eid}/promote-job/job-2", json=payload)

    with session_factory() as db:
        assert len(_notes(db, eid, "job-1")) == 1
        assert len(_notes(db, eid, "job-2")) == 1


def test_acknowledging_an_assessed_job_writes_no_note(client, stub_host, session_factory):
    """The flag is an override for a refusal, not a way to staple a caveat onto a clean scan."""
    stub_host.findings.add_job(
        "job-1",
        engagement_id=CORE, owner_id=7, dtos=[FakeFindingDTO(id=1, title="SQLi")], assessed=True
    )
    _operator(stub_host)
    eid = _engagement(client, stub_host)

    r = client.post(
        f"{M}/engagements/{eid}/promote-job/job-1", json={"acknowledge_inconclusive": True}
    )
    assert r.status_code == 200
    assert r.get_json()["coverage_acknowledged"] is False
    with session_factory() as db:
        assert _notes(db, eid) == []


@pytest.mark.parametrize(
    "payload",
    [
        {},  # the flag is absent -- the overwhelmingly common call, and it must refuse
        {"acknowledge_inconclusive": False},
        {"acknowledge_inconclusive": "false"},
        {"acknowledge_inconclusive": ""},
        {"acknowledge_inconclusive": None},
    ],
    ids=["absent", "json_false", "string_false", "empty_string", "null"],
)
def test_override_is_never_a_default(client, stub_host, payload):
    """`bool("false")` is True. A truthiness parse here would make the string `"false"` an override."""
    stub_host.findings.add_job(
        "job-1",
        engagement_id=CORE,
        owner_id=7,
        dtos=[FakeFindingDTO(id=1)],
        assessed=False,
    )
    _operator(stub_host)
    eid = _engagement(client, stub_host)

    r = client.post(f"{M}/engagements/{eid}/promote-job/job-1", json=payload)
    assert r.status_code == 409, r.get_json()


@pytest.mark.parametrize("raw", ["maybe", "1.5", 1, [], {"nested": True}], ids=str)
def test_unparseable_override_is_a_400_not_a_guess(client, stub_host, raw):
    """Ambiguous consent is refused outright — an operator overriding this has to have SAID so."""
    stub_host.findings.add_job(
        "job-1",
        engagement_id=CORE,
        owner_id=7,
        dtos=[FakeFindingDTO(id=1)],
        assessed=False,
    )
    _operator(stub_host)
    eid = _engagement(client, stub_host)

    r = client.post(
        f"{M}/engagements/{eid}/promote-job/job-1", json={"acknowledge_inconclusive": raw}
    )
    assert r.status_code == 400
    assert r.get_json()["detail"] == "invalid acknowledge_inconclusive"


def test_acknowledged_string_form_is_accepted(client, stub_host):
    """Word forms match `_include_in_report_or_400`'s vocabulary, for callers that can only send text."""
    stub_host.findings.add_job(
        "job-1",
        engagement_id=CORE, owner_id=7, dtos=[FakeFindingDTO(id=1, title="SQLi")], assessed=False
    )
    _operator(stub_host)
    eid = _engagement(client, stub_host)

    r = client.post(
        f"{M}/engagements/{eid}/promote-job/job-1", json={"acknowledge_inconclusive": "true"}
    )
    assert r.status_code == 200


# ── the browser surface ───────────────────────────────────────────────────────────────────────────
#
# `engagement_ui.py` has TWO promote paths, not the one #656 described: `promote_job_ui` (the human
# twin of the machine route) and `adopt_job` (the Source-jobs picker, #630). Both pour a scan job's
# findings onto a report board, so both are boundaries into a client deliverable and both are gated.
# The UI has no flash channel — every refusal in that file is `abort(409, <sentence>)` — so that is
# the shape the operator sees here.

UI = "/scribble/engagements"


def _session_operator(stub_host, uid: int = 1) -> int:
    """Drive as one operator across both identities: the session one the UI routes read, and the PAT
    one used only to create the fixture engagement."""
    from tests.conftest import StubUser, _StubRole

    stub_host.current_user = StubUser(id=uid, username="op", role=_StubRole("operator"))
    stub_host.actor = StubActor(id=uid, username="op", role="operator")
    return uid


@pytest.mark.parametrize("assessed", [False, None], ids=["assessed_false", "assessed_none"])
def test_ui_promote_refuses_an_unassessed_job(client, stub_host, session_factory, assessed):
    uid = _session_operator(stub_host)
    stub_host.findings.add_job(
        "job-1",
        engagement_id=CORE, owner_id=uid, dtos=[FakeFindingDTO(id=1, title="SQLi")],
        assessed=assessed, unassessed_modules=UNASSESSED,
    )
    eid = _engagement(client, stub_host)

    r = client.post(f"{UI}/{eid}/promote-job", data={"job_id": "job-1"})
    assert r.status_code == 409
    # The operator reads this sentence; it must name the modules, not just say no.
    for module in UNASSESSED:
        assert module in r.get_data(as_text=True)

    with session_factory() as db:
        assert _findings(db, eid) == []
    assert stub_host.promoted_calls == []


def test_ui_promote_acknowledged_writes_the_note(client, stub_host, session_factory):
    uid = _session_operator(stub_host)
    stub_host.findings.add_job(
        "job-1",
        engagement_id=CORE, owner_id=uid, dtos=[FakeFindingDTO(id=1, title="SQLi")],
        assessed=False, unassessed_modules=UNASSESSED,
    )
    eid = _engagement(client, stub_host)

    # "on" is what an HTML checkbox actually posts — the browser vocabulary, not the JSON one.
    r = client.post(
        f"{UI}/{eid}/promote-job", data={"job_id": "job-1", "acknowledge_inconclusive": "on"}
    )
    assert r.status_code in (302, 303), r.data

    with session_factory() as db:
        assert "SQLi" in {f.title for f in _findings(db, eid)}
        notes = _notes(db, eid)
        assert len(notes) == 1
        assert all(m in str(notes[0].content_json) for m in UNASSESSED)


def test_ui_promote_assessed_job_still_works(client, stub_host, session_factory):
    uid = _session_operator(stub_host)
    stub_host.findings.add_job(
        "job-1",
        engagement_id=CORE, owner_id=uid, dtos=[FakeFindingDTO(id=1, title="SQLi")], assessed=True
    )
    eid = _engagement(client, stub_host)

    r = client.post(f"{UI}/{eid}/promote-job", data={"job_id": "job-1"})
    assert r.status_code in (302, 303), r.data
    with session_factory() as db:
        assert {f.title for f in _findings(db, eid)} == {"SQLi"}


@pytest.mark.parametrize("assessed", [False, None], ids=["assessed_false", "assessed_none"])
def test_adopt_job_refuses_before_it_links(client, stub_host, session_factory, assessed):
    """The coverage check must precede `mark_job_promoted`, which is this route's gate AND side effect.

    Checking after would leave an unassessed job adopted-but-unpoured — a dangling link the un-adopt
    flow would then have to clean up. `promoted_calls == []` is what pins the ordering.
    """
    uid = _session_operator(stub_host)
    stub_host.findings.add_job(
        "job-1",
        engagement_id=CORE, owner_id=uid, dtos=[FakeFindingDTO(id=1, title="SQLi")],
        assessed=assessed, unassessed_modules=UNASSESSED,
    )
    eid = _engagement(client, stub_host)

    r = client.post(f"{UI}/{eid}/adopt-job/job-1")
    assert r.status_code == 409
    assert stub_host.promoted_calls == []  # NOT linked
    with session_factory() as db:
        assert _findings(db, eid) == []  # NOT poured


def test_adopt_job_acknowledged_links_pours_and_notes(client, stub_host, session_factory):
    uid = _session_operator(stub_host)
    stub_host.findings.add_job(
        "job-1",
        engagement_id=CORE, owner_id=uid, dtos=[FakeFindingDTO(id=1, title="SQLi")],
        assessed=False, unassessed_modules=UNASSESSED,
    )
    eid = _engagement(client, stub_host)

    r = client.post(f"{UI}/{eid}/adopt-job/job-1", data={"acknowledge_inconclusive": "on"})
    assert r.status_code in (302, 303), r.data
    assert len(stub_host.promoted_calls) == 1
    with session_factory() as db:
        assert len(_notes(db, eid)) == 1


def test_adopt_coverage_refusal_is_distinguishable_from_an_adoption_conflict(
    client, stub_host, session_factory
):
    """`adopt-job` is fetch()-driven, and `board.js` previously read 409 as exactly ONE thing.

    Two different 409s that look identical to the client means the operator is told "already adopted
    by another engagement" when the real problem is coverage — a wrong sentence that sends them to fix
    the wrong thing. The `error` code is what keeps them apart, so it is pinned here.
    """
    uid = _session_operator(stub_host)
    stub_host.findings.add_job(
        "job-1",
        engagement_id=CORE, owner_id=uid, dtos=[FakeFindingDTO(id=1)], assessed=False,
        unassessed_modules=UNASSESSED,
    )
    eid = _engagement(client, stub_host)

    r = client.post(f"{UI}/{eid}/adopt-job/job-1")
    assert r.status_code == 409
    assert r.get_json()["error"] == "job_not_assessed"  # NOT the adoption-conflict refusal


@pytest.mark.parametrize("assessed", [False, None], ids=["assessed_false", "assessed_none"])
def test_adopt_by_core_one_click_refuses_before_it_links(client, stub_host, session_factory, assessed):
    """The #847 one-click "Add to report" is the THIRD promote path, and it resolves-or-CREATES a board.

    It reaches the deliverable without ever touching `adopt_job`'s route body, so a guard written only
    into the two routes the issue named would leave this door open. It is also the path where a refusal
    must roll back MORE than a pour: the board itself may have just been created, and an unassessed job
    that leaves a fresh empty board behind has still changed the operator's world.
    """
    uid = _session_operator(stub_host)
    stub_host.engagement_summaries_value = [
        {"id": CORE, "name": "E", "client_id": ACME, "client_name": "Acme"},
    ]
    stub_host.findings.add_job(
        "job-1",
        engagement_id=CORE, owner_id=uid, dtos=[FakeFindingDTO(id=1, title="SQLi")],
        assessed=assessed, unassessed_modules=UNASSESSED,
    )

    r = client.post(f"{UI}/by-core/{CORE}/adopt-job/job-1")
    assert r.status_code == 409
    assert stub_host.promoted_calls == []  # NOT linked
    with session_factory() as db:
        # The board the route was about to create rolled back with the refusal — commit is last. An
        # unassessed job must not leave a fresh empty board behind as its only trace.
        assert db.scalars(select(fm.ReportBoard)).all() == []


def test_adopt_by_core_one_click_acknowledged_pours_and_notes(client, stub_host, session_factory):
    """The override reaches the one-click path too — otherwise its only way forward is another route."""
    uid = _session_operator(stub_host)
    stub_host.engagement_summaries_value = [
        {"id": CORE, "name": "E", "client_id": ACME, "client_name": "Acme"},
    ]
    stub_host.findings.add_job(
        "job-1",
        engagement_id=CORE, owner_id=uid, dtos=[FakeFindingDTO(id=1, title="SQLi")],
        assessed=False, unassessed_modules=UNASSESSED,
    )

    r = client.post(
        f"{UI}/by-core/{CORE}/adopt-job/job-1", data={"acknowledge_inconclusive": "true"}
    )
    assert r.status_code in (302, 303), r.data
    assert len(stub_host.promoted_calls) == 1
    with session_factory() as db:
        board = db.scalars(select(fm.ReportBoard)).one()
        titles = {f.title for f in board.findings}
    assert "SQLi" in titles
    assert COVERAGE_NOTE_TITLE.format(job_id="job-1") in titles


def test_board_js_actually_sends_the_override_the_template_offers():
    """The checkbox and the request that carries it live in two files; only one of them is rendered.

    A box the operator can tick that `board.js` never reads would look exactly like a working override
    and refuse every time. Pinned structurally because no test here executes JavaScript.
    """
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1] / "scribble"
    js = (root / "static" / "board.js").read_text()
    assert "scribble-adopt-ack-inconclusive" in js  # the id the template renders
    assert "acknowledge_inconclusive" in js  # the field name the route reads


def test_the_board_offers_the_override_as_an_unticked_box(client, stub_host, session_factory):
    """A browser operator who hits the refusal needs a way forward that is not hand-crafting a POST.

    Unticked is the assertion that matters: a `checked` attribute here would make the override the
    default and quietly undo the guard for every browser promotion.

    The box lives on the Source-jobs ADOPT picker, which since #234 is the only way a browser reaches a
    promote (the raw job-id form was removed). It is driven by `board.js` over fetch() rather than
    submitted as a form field, so it is addressed by `id` and carries no `name` — the assertion is that
    the affordance EXISTS and is not pre-ticked, not which mechanism posts it.
    """
    _session_operator(stub_host)
    stub_host.viewable_client_ids = stub_host.viewable_client_ids | {ACME}
    eid = _engagement(client, stub_host)

    body = client.get(f"{UI}/{eid}").get_data(as_text=True)
    assert 'id="scribble-adopt-ack-inconclusive"' in body
    box = body[body.index('id="scribble-adopt-ack-inconclusive"') - 60:][:200]
    assert "checked" not in box


def test_ui_unparseable_override_is_a_400(client, stub_host):
    uid = _session_operator(stub_host)
    stub_host.findings.add_job(
        "job-1",
        engagement_id=CORE,
        owner_id=uid,
        dtos=[FakeFindingDTO(id=1)],
        assessed=False,
    )
    eid = _engagement(client, stub_host)

    r = client.post(
        f"{UI}/{eid}/promote-job", data={"job_id": "job-1", "acknowledge_inconclusive": "maybe"}
    )
    assert r.status_code == 400


def test_both_surfaces_refuse_the_same_verdicts(client, stub_host):
    """The machine and browser paths must not drift: same job, same verdict, both refuse."""
    uid = _session_operator(stub_host)
    stub_host.viewable_client_ids = stub_host.viewable_client_ids | {ACME}
    stub_host.findings.add_job(
        "job-1",
        engagement_id=CORE,
        owner_id=uid,
        dtos=[FakeFindingDTO(id=1)],
        assessed=None,
    )
    eid = _engagement(client, stub_host)

    assert client.post(f"{M}/engagements/{eid}/promote-job/job-1").status_code == 409
    assert client.post(f"{UI}/{eid}/promote-job", data={"job_id": "job-1"}).status_code == 409
    assert client.post(f"{UI}/{eid}/adopt-job/job-1").status_code == 409


# ── ordering: the guard must not be reachable around ──────────────────────────────────────────────


def test_tenancy_still_decides_before_coverage(client, stub_host):
    """An unassessed job the caller may not see is 404, not 409.

    Coverage is a property of the job; answering 409 for a job the caller cannot view would leak that
    it exists. The existing no-existence-leak contract outranks the new refusal.
    """
    stub_host.findings.add_job(
        "job-1",
        engagement_id=CORE,
        owner_id=7,
        dtos=[FakeFindingDTO(id=1)],
        assessed=False,
    )
    eid = _engagement(client, stub_host)

    stub_host.actor = StubActor(id=8, username="opB", role="operator")  # doesn't own the job
    assert client.post(f"{M}/engagements/{eid}/promote-job/job-1").status_code == 404


def test_every_promote_surface_consults_the_guard(client, stub_host):
    """No unguarded second door into a deliverable.

    `promote.promote_job` is what turns a scan job into engagement findings. Every module that calls it
    must also consult `scribble.coverage` — otherwise the rule lives in one route's body and the next
    promote surface silently reopens #656 without touching a line of guarded code. This test caught
    exactly that during the build: `engagement_ui.py` had TWO promote paths, not the one the issue
    described, and a later rebase onto `main` turned up a THIRD (`adopt_job_by_core`, the #847 one-click)
    that had landed meanwhile. Both times the file-level sweep is what noticed.

    Asserted structurally rather than by exercising each route, because the failure mode is a route
    that does not exist yet.
    """
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1] / "scribble"
    unguarded = sorted(
        path.name
        for path in root.rglob("*.py")
        if path.name not in {"promote.py", "coverage.py"}
        and "promote_job(" in (text := path.read_text())
        and "coverage.may_promote(" not in text
    )
    assert unguarded == [], (
        f"{unguarded} call promote_job without consulting scribble.coverage.may_promote — "
        "every path into a client deliverable must gate on scan coverage (lotek#656)"
    )
