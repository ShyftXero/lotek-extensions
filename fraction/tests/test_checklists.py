"""Engagement checklists: seeding, snapshot assignment, status rollup, and import/export.

Checklists are non-blocking visual reminders (plans/FRACTION_CHECKLISTS.md). These pin the behaviour that
matters: the seven builtins seed, seeding never clobbers an edited builtin, assignment SNAPSHOTS the
template, status is free text bucketed for report math, and markdown/JSON round-trip.
"""

from __future__ import annotations

from fraction import checklists as C
from fraction.enums import ChecklistKind
from fraction.models import (
    ChecklistTemplate,
    Engagement,
    EngagementChecklist,
)
from fraction.seed import import_checklist_templates

_BUILTIN_SLUGS = {
    "global-pre-engagement",
    "web-app-api",
    "network-infrastructure",
    "ai-llm-security",
    "pci-dss-segmentation",
    "owasp-wstg",
    "owasp-asvs-l1",
}


def _template(session, slug: str) -> ChecklistTemplate:
    return session.query(ChecklistTemplate).filter_by(slug=slug).one()


# --------------------------------------------------------------------------- seeding


def test_seven_builtins_seeded(session_factory):
    with session_factory() as db:
        rows = db.query(ChecklistTemplate).all()
        assert {r.slug for r in rows} == _BUILTIN_SLUGS
        assert all(r.builtin for r in rows)
        assert {r.kind for r in rows} == {
            ChecklistKind.coverage,
            ChecklistKind.reminder,
            ChecklistKind.compliance,
        }


def test_every_builtin_has_items(session_factory):
    with session_factory() as db:
        for r in db.query(ChecklistTemplate).all():
            assert len(r.items) >= 1, f"{r.slug} has no items"


def test_seed_idempotent(session_factory):
    with session_factory() as db:
        before = db.query(ChecklistTemplate).count()
        added = import_checklist_templates(db)  # re-run
        db.commit()
        assert added == 0
        assert db.query(ChecklistTemplate).count() == before


def test_seed_never_clobbers_an_edited_builtin(session_factory):
    # Edit a builtin in place, then re-seed: the edit must survive (never-clobber).
    with session_factory() as db:
        t = _template(db, "web-app-api")
        t.name = "My Custom Web Checklist"
        t.customized = True
        t.items[0].text = "My edited first item"
        db.commit()
    with session_factory() as db:
        added = import_checklist_templates(db)
        db.commit()
        assert added == 0
        t = _template(db, "web-app-api")
        assert t.name == "My Custom Web Checklist"
        assert t.customized is True
        assert t.items[0].text == "My edited first item"


# --------------------------------------------------------------------------- assignment (snapshot)


def _engagement(db) -> Engagement:
    e = Engagement(name="Checklist Eng")
    db.add(e)
    db.flush()
    return e


def test_assign_copies_items_and_defaults(session_factory):
    with session_factory() as db:
        e = _engagement(db)
        t = _template(db, "pci-dss-segmentation")
        ec = C.assign_template(db, e, t, assigned_by="tester")
        db.commit()
        assert ec.kind == ChecklistKind.compliance
        assert len(ec.items) == len(t.items)
        assert ec.items[0].status == "pending"  # kind default
        assert ec.items[0].control_ref == t.items[0].control_ref  # framework mapping carried
        assert ec.assigned_by == "tester"


def test_assignment_is_a_snapshot(session_factory):
    # Editing the template AFTER assign must not mutate the delivered engagement.
    with session_factory() as db:
        e = _engagement(db)
        t = _template(db, "ai-llm-security")
        ec = C.assign_template(db, e, t)
        db.commit()
        original_first = ec.items[0].text
        t.items[0].text = "TEMPLATE EDITED LATER"
        t.name = "TEMPLATE RENAMED"
        db.commit()
    with session_factory() as db:
        ec = db.query(EngagementChecklist).one()
        assert ec.items[0].text == original_first
        assert ec.name != "TEMPLATE RENAMED"


def test_include_in_report_defaults_by_kind(session_factory):
    with session_factory() as db:
        e = _engagement(db)
        cov = C.assign_template(db, e, _template(db, "web-app-api"))
        comp = C.assign_template(db, e, _template(db, "pci-dss-segmentation"))
        rem = C.assign_template(db, e, _template(db, "global-pre-engagement"))
        db.commit()
        assert cov.include_in_report is True
        assert comp.include_in_report is True
        assert rem.include_in_report is False  # reminder is internal by default


def test_zero_or_more_checklists_per_engagement(session_factory):
    with session_factory() as db:
        e = _engagement(db)
        assert e.checklists == []  # zero is fine
        C.assign_template(db, e, _template(db, "web-app-api"))
        C.assign_template(db, e, _template(db, "owasp-wstg"))
        db.commit()
        db.refresh(e)
        assert len(e.checklists) == 2


def test_deleting_engagement_cascades_checklists(session_factory):
    with session_factory() as db:
        e = _engagement(db)
        C.assign_template(db, e, _template(db, "web-app-api"))
        db.commit()
        assert db.query(EngagementChecklist).count() == 1
        db.delete(e)
        db.commit()
        assert db.query(EngagementChecklist).count() == 0


# --------------------------------------------------------------------------- status + rollup


def test_default_status_per_kind():
    assert C.default_status(ChecklistKind.coverage) == "pending"
    assert C.default_status(ChecklistKind.reminder) == "pending"
    assert C.default_status(ChecklistKind.compliance) == "pending"


def test_status_buckets_known_and_custom():
    assert C.status_bucket("pass") == "satisfied"
    assert C.status_bucket("done") == "satisfied"
    assert C.status_bucket("fail") == "deficient"
    assert C.status_bucket("blocked") == "deficient"
    assert C.status_bucket("na") == "not_applicable"
    assert C.status_bucket("pending") == "open"
    assert C.status_bucket("") == "open"
    assert C.status_bucket("Compensating Control") == "open"  # custom -> open


def test_rollup_counts_buckets(session_factory):
    with session_factory() as db:
        e = _engagement(db)
        ec = C.assign_template(db, e, _template(db, "pci-dss-segmentation"))
        db.commit()
        ec.items[0].status = "pass"
        ec.items[1].status = "fail"
        ec.items[2].status = "na"
        ec.items[3].status = "Compensating Control"
        db.commit()
        counts = C.rollup(ec.items)
        assert counts["satisfied"] == 1
        assert counts["deficient"] == 1
        assert counts["not_applicable"] == 1
        assert counts["open"] == len(ec.items) - 3  # remaining pending + the custom label


# --------------------------------------------------------------------------- import / export


def test_markdown_parse_shapes_items():
    md = (
        "# My Checklist\n"
        "*a description*\n\n"
        "## Recon\n"
        "- [ ] **Passive Recon**: do WHOIS and CT logs\n"
        "- [ ] plain bare item\n"
        "## Exploit\n"
        "- [x] **Already Done**: checkbox state ignored on import\n"
    )
    d = C.parse_markdown(md)
    assert d["name"] == "My Checklist"
    assert d["description"] == "a description"
    assert d["kind"] == "coverage"  # markdown carries no kind -> default
    assert len(d["items"]) == 3
    assert d["items"][0]["section"] == "Recon"
    assert d["items"][0]["text"] == "Passive Recon"
    assert d["items"][0]["guidance"] == "do WHOIS and CT logs"
    assert d["items"][1]["text"] == "plain bare item"
    assert d["items"][1]["guidance"] is None
    assert d["items"][2]["section"] == "Exploit"
    assert d["items"][0]["framework"] is None  # markdown has no framework


def test_markdown_parses_bare_bullets_not_only_checkboxes():
    # A non-checkbox list must NOT be silently dropped on import (W2).
    md = "# Plain\n\n## S\n- first bare item\n- second bare item\n* third with asterisk\n"
    d = C.parse_markdown(md)
    assert [i["text"] for i in d["items"]] == ["first bare item", "second bare item", "third with asterisk"]
    # ...but a *italic* description line is still not mistaken for an item.
    d2 = C.parse_markdown("# T\n*just a description*\n\n## S\n- [ ] real item\n")
    assert d2["description"] == "just a description"
    assert [i["text"] for i in d2["items"]] == ["real item"]


def test_markdown_round_trip_preserves_structure():
    md = (
        "# RT\n\n## S1\n- [ ] **A**: guide a\n- [ ] **B**: guide b\n"
        "## S2\n- [ ] **C**: guide c\n"
    )
    d1 = C.parse_markdown(md)
    d2 = C.parse_markdown(C.to_markdown(d1))
    got1 = [(i["section"], i["text"], i["guidance"]) for i in d1["items"]]
    got2 = [(i["section"], i["text"], i["guidance"]) for i in d2["items"]]
    assert got1 == got2


def test_json_round_trip_is_lossless(session_factory):
    with session_factory() as db:
        t = _template(db, "pci-dss-segmentation")
        d = C.template_to_dict(t)
        nd = C.normalize_template_dict(d)
        assert nd["kind"] == "compliance"
        assert nd["name"] == t.name
        assert len(nd["items"]) == len(t.items)
        # framework + control_ref survive the round trip
        assert nd["items"][0]["framework"] == t.items[0].framework
        assert nd["items"][0]["control_ref"] == t.items[0].control_ref


def test_normalize_coerces_unknown_kind_and_drops_empty_items():
    nd = C.normalize_template_dict(
        {"name": "X", "kind": "not-a-kind", "items": [{"text": ""}, {"text": "keep me"}]}
    )
    assert nd["kind"] == "coverage"
    assert len(nd["items"]) == 1
    assert nd["items"][0]["text"] == "keep me"
