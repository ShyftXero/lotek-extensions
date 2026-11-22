"""Walker/loader self-check: import_vulns_from_toml_dir must parse every file in seed/vulns/
and produce non-empty VulnerabilityTemplate rows idempotent across two calls. Fails if any TOML
file is malformed, if a required field is missing, or if the DB round-trip breaks."""

from __future__ import annotations

import tomllib

from scribble.seed.loader import _VULNS_DIR, import_vulns_from_toml_dir

_EXPECTED_COUNT = 20


def test_vulns_dir_has_expected_toml_count():
    paths = list(_VULNS_DIR.rglob("*.toml"))
    assert len(paths) == _EXPECTED_COUNT, (
        f"seed/vulns/ has {len(paths)} TOML files, expected {_EXPECTED_COUNT}; "
        "update _EXPECTED_COUNT when adding/removing a template"
    )


def test_every_toml_file_parses_and_has_required_fields():
    for path in _VULNS_DIR.rglob("*.toml"):
        rec = tomllib.loads(path.read_text())
        assert rec.get("name"), f"{path.name}: missing 'name'"
        assert rec.get("severity"), f"{path.name}: missing 'severity'"
        assert rec.get("description") or rec.get("content_json"), (
            f"{path.name}: no 'description' or 'content_json'"
        )


def test_import_vulns_from_toml_dir_seeds_all_templates(session_factory):
    with session_factory() as db:
        added = import_vulns_from_toml_dir(db, _VULNS_DIR)
        db.commit()
    assert added == _EXPECTED_COUNT

    # Idempotent: a second call adds nothing.
    with session_factory() as db:
        added2 = import_vulns_from_toml_dir(db, _VULNS_DIR)
        db.commit()
    assert added2 == 0
