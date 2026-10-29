"""The board's job-adopt picker (#234): one searchable control instead of two opaque-UUID fields.

The board used to offer two ways to attach a scan job — a toolbar "Promote a scan job" and a Source-jobs
"Adopt" — both a raw job-id text field, both routing through the same `_adopt_job_onto_board`. This
collapses them to ONE searchable picker (core combobox in data-search-url mode) backed by
`adoptable_jobs_json` -> `host.adoptable_jobs`, so an operator picks a job by name/target, never a UUID.
"""
from __future__ import annotations

import uuid

import scribble.models as fm

UI = "/scribble"


def _board(session_factory) -> uuid.UUID:
    with session_factory() as db:
        b = fm.ReportBoard(name="Q1 Assessment", scope_type="external")
        db.add(b)
        db.commit()
        return b.id


def test_adoptable_jobs_json_returns_combobox_items(client, stub_host, session_factory):
    bid = _board(session_factory)
    job_id = str(uuid.uuid7())
    stub_host.adoptable_jobs_value = [
        {"id": job_id, "name": "Recon scanme", "targets": "scanme.nmap.org\n10.0.0.1",
         "status": "completed", "created_at": "2026-09-24T06:00:00",
         "findings_total": 3, "severity_counts": {"critical": 1, "medium": 2}},
    ]
    r = client.get(f"{UI}/engagements/{bid}/adoptable-jobs.json")
    assert r.status_code == 200
    items = r.get_json()["items"]
    assert len(items) == 1
    it = items[0]
    assert it["value"] == job_id, "the picker submits the job id as the selected value"
    # label is human-readable: name + first target line + status + date, never the bare UUID
    assert "Recon scanme" in it["label"]
    assert "scanme.nmap.org" in it["label"] and "10.0.0.1" not in it["label"]  # first target line only
    assert "completed" in it["label"] and "2026-09-24" in it["label"]
    # finding/severity count shows what you're adopting before you adopt it
    assert "3 findings (1C 2M)" in it["label"]
    assert job_id not in it["label"]


def test_board_adopt_is_a_searchable_picker_not_a_uuid_field(client, stub_host, session_factory):
    bid = _board(session_factory)
    body = client.get(f"{UI}/engagements/{bid}").get_data(as_text=True)
    # the adopt control is a combobox wired to the search endpoint
    assert 'id="scribble-adopt-job-id"' in body and "data-combobox" in body
    assert f"/scribble/engagements/{bid}/adoptable-jobs.json" in body
    # the old raw-UUID inputs are gone (Source-jobs field + the redundant toolbar "Promote" affordance)
    assert 'placeholder="Scan job id to adopt"' not in body
    assert "Promote a scan job" not in body
    assert 'name="job_id"' not in body  # no bare job-id text field anywhere on the board


def test_already_adopted_jobs_are_badged_in_the_picker(client, stub_host, session_factory):
    """#616 item 2: a job already adopted is marked so an operator doesn't adopt it twice.

    Adoption state lives on the core Job row and the host hook carries it (promoted_extension/
    promoted_ref_id). The picker distinguishes adopted-HERE (ref == this board) from adopted-ELSEWHERE,
    and an un-adopted job carries no marker.
    """
    bid = _board(session_factory)
    here, elsewhere, free = str(uuid.uuid7()), str(uuid.uuid7()), str(uuid.uuid7())
    stub_host.adoptable_jobs_value = [
        {"id": here, "name": "Adopted here", "targets": "a", "status": "completed",
         "created_at": "2026-09-24T06:00:00", "findings_total": 0, "severity_counts": {},
         "promoted_extension": "scribble", "promoted_ref_id": str(bid), "promoted_at": "2026-09-25T00:00:00"},
        {"id": elsewhere, "name": "Adopted elsewhere", "targets": "b", "status": "completed",
         "created_at": "2026-09-24T06:00:00", "findings_total": 0, "severity_counts": {},
         "promoted_extension": "scribble", "promoted_ref_id": str(uuid.uuid7()), "promoted_at": "2026-09-25T00:00:00"},
        {"id": free, "name": "Fresh recon", "targets": "c", "status": "completed",
         "created_at": "2026-09-24T06:00:00", "findings_total": 0, "severity_counts": {}},
    ]
    by_id = {it["value"]: it["label"] for it in
             client.get(f"{UI}/engagements/{bid}/adoptable-jobs.json").get_json()["items"]}
    assert by_id[here].startswith("✓ adopted here")
    assert by_id[elsewhere].startswith("⛔ adopted elsewhere")
    assert "adopted" not in by_id[free].lower()  # an un-adopted job carries no marker


def test_inconclusive_refusal_is_an_override_modal(client, stub_host, session_factory):
    """#616 item 1: the inconclusive-coverage refusal is a prominent modal whose checkbox enables
    "Adopt anyway" (disabled by default); Cancel is the default. No JS runs here, so pin it structurally
    across the two files that must agree — the template renders the dialog, board.js drives it.
    """
    import pathlib

    bid = _board(session_factory)
    body = client.get(f"{UI}/engagements/{bid}").get_data(as_text=True)
    assert '<dialog id="scribble-adopt-refusal"' in body
    assert 'id="scribble-adopt-ack-inconclusive"' in body  # the enabling checkbox
    go = body[body.index('id="scribble-adopt-refusal-go"'):][:120]
    assert "disabled" in go  # "Adopt anyway" starts disabled until the box is ticked
    assert 'id="scribble-adopt-refusal-cancel"' in body and "autofocus" in body  # Cancel is the default

    js = (pathlib.Path(__file__).resolve().parents[1] / "scribble" / "static" / "board.js").read_text()
    assert "scribble-adopt-refusal" in js and "showModal" in js  # board.js opens the dialog
    assert "job_not_assessed" in js  # only the coverage refusal pops the override modal
