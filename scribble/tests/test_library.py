"""WS2 tests: vulnerability template library CRUD (scribble/library_ui.py).

Fixtures (`app`, `client`, `session_factory`) come from `tests/conftest.py`: `scribble.register()` wires
every feature workstream's `register(api_bp, bp)` hook (including `library_ui.register`) onto the shared
blueprint singletons before the app registers them, so these tests don't need to build their own app —
unlike `tests/test_artifacts.py`/`tests/test_editor.py`, WS2's routes are already reachable through the
standard fixtures the same way `tests/test_smoke.py` exercises `scribble.library`.

Covers: edit-in-place (same row id, fields changed, content_html re-cached), duplicate (new row, copied
content), create, delete/deactivate (soft, reversible), tag assign (existing + create-and-assign), the
template-scoped block read/write endpoints (mirroring autosave_api's contract but keyed on
VulnerabilityTemplate instead of EngagementFinding), and list search/filter.
"""

from __future__ import annotations

import pytest

from scribble import library_ui
from scribble.api import api_bp
from scribble.blueprint import bp
from scribble.content import schema
from scribble.content.render_html import render_block
from scribble.enums import Severity
from scribble.models import Tag, VulnerabilityTemplate

API_PREFIX = "/scribble/api"

SAMPLE_DOC = {
    "type": "doc",
    "content": [
        {
            "type": "paragraph",
            "content": [
                {"type": "text", "text": "Patch the affected "},
                {"type": "variable", "attrs": {"key": "TARGET_HOST"}},
            ],
        }
    ],
}


def _block_url(template_id: int, block: str) -> str:
    return f"{API_PREFIX}/templates/{template_id}/blocks/{block}"


def _make_template(db, **overrides) -> VulnerabilityTemplate:
    defaults = dict(name="zzTest Template", category="zzTestCat", default_severity=Severity.medium)
    defaults.update(overrides)
    t = VulnerabilityTemplate(**defaults)
    db.add(t)
    db.commit()
    return t


# --------------------------------------------------------------------------------------- register()


def test_register_is_idempotent():
    library_ui.register(api_bp, bp)
    library_ui.register(api_bp, bp)


# --------------------------------------------------------------------------------------- pages


def test_library_page_renders(client):
    resp = client.get("/scribble/library")
    assert resp.status_code == 200


def test_library_new_page_renders(client):
    resp = client.get("/scribble/library/new")
    assert resp.status_code == 200


def test_library_detail_page_renders(client, session_factory):
    with session_factory() as db:
        t = _make_template(db, name="zzDetail View")
        template_id = t.id
    resp = client.get(f"/scribble/library/{template_id}")
    assert resp.status_code == 200
    assert b"zzDetail View" in resp.data


def test_library_detail_page_404_for_missing_id(client):
    resp = client.get("/scribble/library/999999")
    assert resp.status_code == 404


# --------------------------------------------------------------------------------------- search/filter


def test_library_search_by_name(client, session_factory):
    with session_factory() as db:
        _make_template(db, name="Zzyzx Unique Vuln Name")
    resp = client.get("/scribble/library", query_string={"q": "Zzyzx Unique"})
    assert resp.status_code == 200
    assert b"Zzyzx Unique Vuln Name" in resp.data


def test_library_search_excludes_non_matching(client, session_factory):
    with session_factory() as db:
        _make_template(db, name="zzAlpha One")
        _make_template(db, name="zzBeta Two")
    resp = client.get("/scribble/library", query_string={"q": "zzAlpha"})
    assert b"zzAlpha One" in resp.data
    assert b"zzBeta Two" not in resp.data


def test_library_filter_by_category(client, session_factory):
    with session_factory() as db:
        _make_template(db, name="zzCatA Item", category="zzCatA")
        _make_template(db, name="zzCatB Item", category="zzCatB")
    resp = client.get("/scribble/library", query_string={"category": "zzCatA"})
    assert b"zzCatA Item" in resp.data
    assert b"zzCatB Item" not in resp.data


def test_library_filter_by_severity(client, session_factory):
    with session_factory() as db:
        _make_template(db, name="zzSevCrit", default_severity=Severity.critical)
        _make_template(db, name="zzSevLow", default_severity=Severity.low)
    resp = client.get("/scribble/library", query_string={"severity": "critical"})
    assert b"zzSevCrit" in resp.data
    assert b"zzSevLow" not in resp.data


def test_library_filter_by_tag(client, session_factory):
    create_a = client.post(
        f"{API_PREFIX}/templates", json={"name": "zzTag Web Finding", "tags": ["zzweb", "zzinjection"]}
    )
    create_b = client.post(
        f"{API_PREFIX}/templates", json={"name": "zzTag Other Finding", "tags": ["zzother"]}
    )
    assert create_a.status_code == 201
    assert create_b.status_code == 201

    resp = client.get("/scribble/library", query_string={"tag": "zzweb"})
    assert b"zzTag Web Finding" in resp.data
    assert b"zzTag Other Finding" not in resp.data


def test_library_excludes_inactive_by_default_and_toggle_shows_them(client, session_factory):
    with session_factory() as db:
        _make_template(db, name="zzInactive One", active=False)

    default_resp = client.get("/scribble/library")
    assert b"zzInactive One" not in default_resp.data

    toggled_resp = client.get("/scribble/library", query_string={"inactive": "1"})
    assert b"zzInactive One" in toggled_resp.data


# --------------------------------------------------------------------------------------- create


def test_create_template_minimal(client, session_factory):
    resp = client.post(f"{API_PREFIX}/templates", json={"name": "zzNew Template"})
    assert resp.status_code == 201
    data = resp.get_json()
    assert data["ok"] is True
    assert data["id"] is not None
    assert data["redirect"].endswith(f"/scribble/library/{data['id']}")

    with session_factory() as db:
        t = db.get(VulnerabilityTemplate, data["id"])
        assert t.name == "zzNew Template"
        assert t.active is True
        assert t.default_severity == Severity.medium
        assert t.content_json == {}


def test_create_template_full_fields(client, session_factory):
    resp = client.post(
        f"{API_PREFIX}/templates",
        json={
            "name": "zzFull Template",
            "category": "zzInjection",
            "default_severity": "high",
            "cvss_score": 7.5,
            "cvss_vector": "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:N",
            "references": ["https://example.com/a", "https://example.com/b"],
            "tags": ["zzsqli", "zzweb"],
        },
    )
    assert resp.status_code == 201
    template_id = resp.get_json()["id"]

    with session_factory() as db:
        t = db.get(VulnerabilityTemplate, template_id)
        assert t.category == "zzInjection"
        assert t.default_severity == Severity.high
        assert t.cvss_score == 7.5
        assert t.references == ["https://example.com/a", "https://example.com/b"]
        assert {tag.name for tag in t.tags} == {"zzsqli", "zzweb"}


def test_create_template_requires_name(client):
    resp = client.post(f"{API_PREFIX}/templates", json={"name": "   "})
    assert resp.status_code == 400
    assert resp.get_json()["ok"] is False


def test_create_template_rejects_invalid_severity(client):
    resp = client.post(
        f"{API_PREFIX}/templates", json={"name": "zzBad Sev", "default_severity": "apocalyptic"}
    )
    assert resp.status_code == 400


def test_create_reuses_existing_tag_row(client, session_factory):
    client.post(f"{API_PREFIX}/templates", json={"name": "zzTagReuseA", "tags": ["zzshared-tag"]})
    client.post(f"{API_PREFIX}/templates", json={"name": "zzTagReuseB", "tags": ["zzshared-tag"]})
    with session_factory() as db:
        assert db.query(Tag).filter_by(name="zzshared-tag").count() == 1


# --------------------------------------------------------------------------------------- edit-in-place


def test_update_edits_same_row_in_place(client, session_factory):
    with session_factory() as db:
        t = _make_template(db, name="zzOriginal Name", category="zzOriginalCat")
        template_id = t.id
        count_before = db.query(VulnerabilityTemplate).count()

    resp = client.post(
        f"{API_PREFIX}/templates/{template_id}",
        json={"name": "zzRenamed", "category": "zzNewCat", "default_severity": "critical"},
    )
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert data["id"] == template_id  # same row, no forced duplicate

    with session_factory() as db:
        # No new row was created by "edit" -- the total row count is unchanged.
        assert db.query(VulnerabilityTemplate).count() == count_before
        reloaded = db.get(VulnerabilityTemplate, template_id)
        assert reloaded.id == template_id
        assert reloaded.name == "zzRenamed"
        assert reloaded.category == "zzNewCat"
        assert reloaded.default_severity == Severity.critical


def test_update_recaches_content_html_from_content_json(client, session_factory):
    # Simulate content that was written directly to content_json without a cached HTML render
    # (e.g. a fixture, or content_html having gone stale) -- saving the template metadata must
    # re-derive + cache content_html per block, same as the per-block save endpoint does.
    with session_factory() as db:
        t = _make_template(db, name="zzStaleHtml", content_json={"description": SAMPLE_DOC}, content_html={})
        template_id = t.id

    resp = client.post(f"{API_PREFIX}/templates/{template_id}", json={"category": "zzRecached"})
    assert resp.status_code == 200

    with session_factory() as db:
        reloaded = db.get(VulnerabilityTemplate, template_id)
        html = reloaded.content_html.get("description", "")
        assert "Patch the affected" in html
        assert "{{TARGET_HOST}}" in html  # unresolved editor-preview render, matches autosave_api


def test_update_missing_template_404(client):
    resp = client.post(f"{API_PREFIX}/templates/999999", json={"name": "x"})
    assert resp.status_code == 404


def test_update_rejects_empty_name(client, session_factory):
    with session_factory() as db:
        t = _make_template(db, name="zzKeepName")
        template_id = t.id
    resp = client.post(f"{API_PREFIX}/templates/{template_id}", json={"name": "   "})
    assert resp.status_code == 400
    with session_factory() as db:
        assert db.get(VulnerabilityTemplate, template_id).name == "zzKeepName"


def test_update_rejects_invalid_severity(client, session_factory):
    with session_factory() as db:
        t = _make_template(db, name="zzSevGuard")
        template_id = t.id
    resp = client.post(f"{API_PREFIX}/templates/{template_id}", json={"default_severity": "nonsense"})
    assert resp.status_code == 400


def test_update_replaces_tags(client, session_factory):
    with session_factory() as db:
        t = _make_template(db, name="zzTagSwap")
        template_id = t.id

    client.post(f"{API_PREFIX}/templates/{template_id}", json={"tags": ["zzA", "zzB"]})
    with session_factory() as db:
        assert {tag.name for tag in db.get(VulnerabilityTemplate, template_id).tags} == {"zzA", "zzB"}

    client.post(f"{API_PREFIX}/templates/{template_id}", json={"tags": ["zzB", "zzC"]})
    with session_factory() as db:
        assert {tag.name for tag in db.get(VulnerabilityTemplate, template_id).tags} == {"zzB", "zzC"}


def test_update_can_reactivate(client, session_factory):
    with session_factory() as db:
        t = _make_template(db, name="zzReactivate", active=False)
        template_id = t.id
    resp = client.post(f"{API_PREFIX}/templates/{template_id}", json={"active": True})
    assert resp.status_code == 200
    with session_factory() as db:
        assert db.get(VulnerabilityTemplate, template_id).active is True


# --------------------------------------------------------------------------------------- duplicate


def test_duplicate_creates_new_row_with_copied_content(client, session_factory):
    with session_factory() as db:
        original = _make_template(
            db,
            name="zzOriginal For Dup",
            category="zzDupCat",
            default_severity=Severity.high,
            cvss_score=6.4,
            cvss_vector="CVSS:3.1/AV:N",
            references=["https://example.com/ref"],
        )
        original.content_json = {"description": SAMPLE_DOC}
        original.content_html = {"description": render_block(SAMPLE_DOC)}
        db.commit()
        original_id = original.id

    client.post(f"{API_PREFIX}/templates/{original_id}", json={"tags": ["zzduptag"]})

    resp = client.post(f"{API_PREFIX}/templates/{original_id}/duplicate")
    assert resp.status_code == 201
    data = resp.get_json()
    assert data["ok"] is True
    new_id = data["id"]
    assert new_id != original_id  # a genuinely new row

    with session_factory() as db:
        original_reloaded = db.get(VulnerabilityTemplate, original_id)
        dup = db.get(VulnerabilityTemplate, new_id)

        # Original untouched.
        assert original_reloaded.id == original_id
        assert original_reloaded.name == "zzOriginal For Dup"

        # Duplicate forked with " (copy)" suffix and copied fields.
        assert dup.name == "zzOriginal For Dup (copy)"
        assert dup.category == "zzDupCat"
        assert dup.default_severity == Severity.high
        assert dup.cvss_score == 6.4
        assert dup.cvss_vector == "CVSS:3.1/AV:N"
        assert dup.references == ["https://example.com/ref"]
        assert dup.active is True
        assert {t.name for t in dup.tags} == {"zzduptag"}
        assert dup.content_json == {"description": SAMPLE_DOC}
        assert "Patch the affected" in dup.content_html["description"]

        # Independent copies: mutating the duplicate's content must not affect the original's.
        dup.content_json["description"]["content"][0]["content"][0]["text"] = "mutated"
        db.commit()
    with session_factory() as db:
        original_after = db.get(VulnerabilityTemplate, original_id)
        assert original_after.content_json["description"]["content"][0]["content"][0]["text"] == (
            "Patch the affected "
        )


def test_duplicate_of_template_with_empty_content(client, session_factory):
    with session_factory() as db:
        t = _make_template(db, name="zzEmptyContentDup")
        template_id = t.id
    resp = client.post(f"{API_PREFIX}/templates/{template_id}/duplicate")
    assert resp.status_code == 201


def test_duplicate_missing_template_404(client):
    resp = client.post(f"{API_PREFIX}/templates/999999/duplicate")
    assert resp.status_code == 404


# --------------------------------------------------------------------------------------- delete/deactivate


def test_delete_deactivates_without_removing_row(client, session_factory):
    with session_factory() as db:
        t = _make_template(db, name="zzToDeactivate")
        template_id = t.id

    resp = client.post(f"{API_PREFIX}/templates/{template_id}/delete")
    assert resp.status_code == 200
    assert resp.get_json()["ok"] is True

    with session_factory() as db:
        reloaded = db.get(VulnerabilityTemplate, template_id)
        assert reloaded is not None  # soft delete: row still exists
        assert reloaded.active is False


def test_delete_toggles_active_reactivates(client, session_factory):
    # /delete toggles: first call deactivates, second reactivates (the detail-page button relies on
    # this so a template labeled "Reactivate" actually comes back).
    with session_factory() as db:
        t = _make_template(db, name="zzToggle")
        template_id = t.id

    first = client.post(f"{API_PREFIX}/templates/{template_id}/delete")
    assert first.get_json()["active"] is False
    second = client.post(f"{API_PREFIX}/templates/{template_id}/delete")
    assert second.get_json()["active"] is True

    with session_factory() as db:
        assert db.get(VulnerabilityTemplate, template_id).active is True


def test_duplicate_preserves_inactive_state(client, session_factory):
    # Duplicating a deactivated template must NOT silently resurrect it as active.
    with session_factory() as db:
        t = _make_template(db, name="zzInactiveSource")
        t.active = False
        db.commit()
        template_id = t.id

    resp = client.post(f"{API_PREFIX}/templates/{template_id}/duplicate")
    assert resp.status_code == 201
    dup_id = resp.get_json()["id"]

    with session_factory() as db:
        assert db.get(VulnerabilityTemplate, dup_id).active is False


def test_delete_missing_template_404(client):
    resp = client.post(f"{API_PREFIX}/templates/999999/delete")
    assert resp.status_code == 404


# --------------------------------------------------------------------------------------- content blocks


def test_save_block_stores_json_and_caches_html(client, session_factory):
    with session_factory() as db:
        t = _make_template(db, name="zzBlockSave")
        template_id = t.id

    resp = client.post(_block_url(template_id, "description"), json=SAMPLE_DOC)
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert "Patch the affected" in data["html"]
    assert "{{TARGET_HOST}}" in data["html"]

    with session_factory() as db:
        reloaded = db.get(VulnerabilityTemplate, template_id)
        assert reloaded.content_json["description"] == SAMPLE_DOC
        assert "Patch the affected" in reloaded.content_html["description"]


def test_save_block_only_touches_target_block(client, session_factory):
    with session_factory() as db:
        t = _make_template(db, name="zzBlockIsolation")
        template_id = t.id

    client.post(_block_url(template_id, "description"), json=SAMPLE_DOC)
    other_doc = schema.doc_from_text("Remediate by patching.")
    client.post(_block_url(template_id, "remediation"), json=other_doc)

    with session_factory() as db:
        reloaded = db.get(VulnerabilityTemplate, template_id)
        assert reloaded.content_json["description"] == SAMPLE_DOC
        assert reloaded.content_json["remediation"] == other_doc
        assert "Remediate" in reloaded.content_html["remediation"]


def test_get_block_round_trips(client, session_factory):
    with session_factory() as db:
        t = _make_template(db, name="zzBlockRoundtrip")
        template_id = t.id

    assert client.post(_block_url(template_id, "description"), json=SAMPLE_DOC).status_code == 200
    resp = client.get(_block_url(template_id, "description"))
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["doc"] == SAMPLE_DOC
    assert "Patch the affected" in data["html"]


def test_get_unwritten_block_returns_empty_doc(client, session_factory):
    with session_factory() as db:
        t = _make_template(db, name="zzBlockEmpty")
        template_id = t.id
    resp = client.get(_block_url(template_id, "remediation"))
    assert resp.status_code == 200
    data = resp.get_json()
    assert schema.is_doc(data["doc"])
    assert data["doc"]["content"] == []
    assert data["html"] == ""


@pytest.mark.parametrize(
    "body",
    [
        {"type": "paragraph", "content": []},
        {"content": []},
        ["not", "an", "object"],
        "just a string",
        None,
        123,
    ],
)
def test_save_block_rejects_malformed_body(client, session_factory, body):
    with session_factory() as db:
        t = _make_template(db, name="zzBlockMalformed")
        template_id = t.id
    resp = client.post(_block_url(template_id, "description"), json=body)
    assert resp.status_code == 400
    assert resp.get_json()["ok"] is False


def test_save_block_missing_template_404(client):
    resp = client.post(_block_url(999999, "description"), json=SAMPLE_DOC)
    assert resp.status_code == 404


def test_get_block_missing_template_404(client):
    resp = client.get(_block_url(999999, "description"))
    assert resp.status_code == 404
