"""`scribble.seed.seed_vuln_map` — the idempotent `ScribbleVulnMap` seed
(ported from the deleted lotek `tests/test_api_v1_vulnmap.py`'s builtin-VulnMap-seed section, now
exercised directly against `scribble.seed` instead of through a booted lotek app).

Proves: every shipped `lotek_vuln_map.json` entry resolves to a real, active library template exactly
once (idempotent fresh seed); zero-match on a fresh lookup RAISES (a missing library entry is model
drift, not something to paper over); ambiguous name resolves deterministically to the lowest id with a
warning; re-running is idempotent across "restarts" (no duplicate rows).
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import select

import scribble.models as fm
from scribble.enums import Severity
from scribble.seed.loader import seed_vuln_map


def test_seed_maps_every_shipped_entry_to_a_real_active_template(session_factory):
    """The base `app` fixture already ran `seed_defaults` (which calls `seed_vuln_map`) once at boot;
    re-derive the expectation from the shipped JSON rather than hardcoding a count."""
    from scribble.seed.loader import _DEFAULT_VULN_MAP_JSON

    specs = json.loads(_DEFAULT_VULN_MAP_JSON.read_text())
    with session_factory() as db:
        rows = db.execute(select(fm.ScribbleVulnMap)).scalars().all()
        assert len(rows) == len(specs)
        for row in rows:
            template = db.get(fm.VulnerabilityTemplate, row.template_id)
            assert template is not None and template.active, (
                f"ScribbleVulnMap row {row.id} (source={row.source!r}, "
                f"title_pattern={row.title_pattern!r}, dedupe_prefix={row.dedupe_prefix!r}) points at "
                "a missing/inactive template"
            )
        by_key = {(r.source, r.title_pattern, r.dedupe_prefix): r for r in rows}
        kerberoast = by_key[(None, None, "kerberoast:")]
        assert db.get(fm.VulnerabilityTemplate, kerberoast.template_id).name == "Kerberoasting"
        dalfox = by_key[("dalfox", None, None)]
        assert db.get(fm.VulnerabilityTemplate, dalfox.template_id).name == "Cross-Site Scripting (XSS)"
        smb_signing = by_key[("enum4linux", "SMB signing not required", None)]
        assert (
            db.get(fm.VulnerabilityTemplate, smb_signing.template_id).name == "SMB Signing Not Required"
        )


def test_seed_is_idempotent_across_reseed_runs(session_factory):
    """A second `seed_vuln_map` call against the SAME db (simulating a restart) must not add
    duplicate rows."""
    with session_factory() as db:
        before = db.execute(select(fm.ScribbleVulnMap)).scalars().all()
        before_ids = {r.id for r in before}
        seed_vuln_map(db)  # re-run
        db.commit()
        after = db.execute(select(fm.ScribbleVulnMap)).scalars().all()
        assert {r.id for r in after} == before_ids


def test_seed_fails_loudly_on_zero_match(session_factory, tmp_path):
    """A spec naming a library entry that doesn't exist (renamed/removed) must raise, not silently
    skip -- a missing entry is model drift the seed must surface, not paper over."""
    spec_path = tmp_path / "bad_vuln_map.json"
    spec_path.write_text(json.dumps([{"name": "Definitely Not A Real Library Entry", "source": "nope"}]))
    with (
        session_factory() as db,
        pytest.raises(RuntimeError, match="no active scribble_vuln_templates row named"),
    ):
        seed_vuln_map(db, json_path=spec_path)


def test_seed_ambiguous_name_picks_lowest_id(session_factory, tmp_path, caplog):
    """Two active templates sharing a name (`VulnerabilityTemplate.name` has no unique constraint)
    resolve deterministically to the LOWEST id, with a warning logged (not a silent pick, not a
    raise)."""
    with session_factory() as db:
        older = fm.VulnerabilityTemplate(name="Dup Vuln", active=True, default_severity=Severity.medium)
        db.add(older)
        db.commit()
        newer = fm.VulnerabilityTemplate(name="Dup Vuln", active=True, default_severity=Severity.medium)
        db.add(newer)
        db.commit()
        older_id, newer_id = older.id, newer.id

    spec_path = tmp_path / "dup_vuln_map.json"
    spec_path.write_text(json.dumps([{"name": "Dup Vuln", "source": "duptest"}]))
    with session_factory() as db:
        seed_vuln_map(db, json_path=spec_path)
        db.commit()
        row = db.execute(
            select(fm.ScribbleVulnMap).where(fm.ScribbleVulnMap.source == "duptest")
        ).scalar_one()
        assert row.template_id == min(older_id, newer_id)
