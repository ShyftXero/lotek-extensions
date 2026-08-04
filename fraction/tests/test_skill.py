"""WS9: light checks for the fraction-report-refine Claude skill.

Not an integration test of the skill's AI behavior (that can't be pinned in CI) — just the two things
that must always be true for the skill to be usable at all: ``SKILL.md`` has valid, non-empty
frontmatter, and its read-only sidecar helper (``scripts/context_sidecar.py``) actually produces the
``ReportContext``-shaped JSON the skill's input contract promises, without writing to the database.
"""

from __future__ import annotations

import importlib.util
import json
import re
import sqlite3
from pathlib import Path
from types import ModuleType

import pytest
from sqlalchemy import create_engine

from fraction.db import create_all, make_session_factory
from fraction.seed import seed_defaults
from fraction.seed.demo import seed_demo

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SKILL_DIR = _REPO_ROOT / "skill" / "fraction-report-refine"
_SKILL_MD = _SKILL_DIR / "SKILL.md"
_SIDECAR_SCRIPT = _SKILL_DIR / "scripts" / "context_sidecar.py"
_METHODOLOGY = _SKILL_DIR / "references" / "methodology.md"


def _load_sidecar_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("fraction_report_refine_context_sidecar", _SIDECAR_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _parse_frontmatter(text: str) -> dict[str, str]:
    """Minimal ``key: value`` frontmatter parse — no PyYAML dependency needed for this light check."""
    match = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
    assert match, "SKILL.md must start with a --- frontmatter block"
    fields: dict[str, str] = {}
    for line in match.group(1).splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        fields[key.strip()] = value.strip()
    return fields


def _seed_demo_db(db_path: Path) -> int:
    """Create + seed a demo Fraction sqlite file (writes are expected here); return the engagement id."""
    engine = create_engine(f"sqlite:///{db_path}", future=True)
    create_all(engine)
    session_factory = make_session_factory(engine)
    with session_factory() as session:
        seed_defaults(session)
        session.commit()
        engagement = seed_demo(session)
        session.commit()
        engagement_id = engagement.id
    engine.dispose()
    return engagement_id


# --------------------------------------------------------------------------------- SKILL.md structure


def test_skill_md_exists_with_valid_frontmatter():
    assert _SKILL_MD.is_file(), "SKILL.md must exist under skill/fraction-report-refine/"
    fields = _parse_frontmatter(_SKILL_MD.read_text(encoding="utf-8"))

    assert fields.get("name") == "fraction-report-refine"
    description = fields.get("description", "")
    assert len(description) > 40, "description must be a real, precise trigger description"
    # Trigger phrases named in the WS9 spec so the skill actually fires on the expected prompts.
    assert "refine the fraction report" in description.lower()
    assert "executive" in description.lower() and "narrative" in description.lower()


def test_skill_md_states_the_no_data_change_guardrail():
    body = _SKILL_MD.read_text(encoding="utf-8").lower()
    # The non-negotiable guardrail must be explicit, not implied — an adversarial read of this file
    # must not be able to construe it as license to alter findings data.
    assert "severity" in body and "cvss" in body and "evidence" in body
    assert "never" in body
    assert "guardrail" in body


def test_methodology_reference_covers_seeded_assessment_types():
    assert _METHODOLOGY.is_file()
    text = _METHODOLOGY.read_text(encoding="utf-8").lower()
    for slug in ("internal", "external", "web-app", "device-mobile"):
        assert slug in text


# --------------------------------------------------------------------------------- context_sidecar.py


def test_context_sidecar_produces_report_context_shape(tmp_path):
    db_path = tmp_path / "fraction.db"
    engagement_id = _seed_demo_db(db_path)

    sidecar = _load_sidecar_module()
    with sidecar.open_readonly_session(db_path) as session:
        data = sidecar.build_sidecar_dict(session, engagement_id=engagement_id)

    assert isinstance(data, dict)
    for key in ("groups", "rollup", "variables", "engagement_id", "engagement_name"):
        assert key in data, f"sidecar dict missing {key!r}"

    assert data["engagement_id"] == engagement_id
    assert isinstance(data["groups"], list) and len(data["groups"]) > 0
    assert isinstance(data["variables"], dict)
    assert isinstance(data["rollup"], dict)
    for rollup_key in ("counts", "total", "overall"):
        assert rollup_key in data["rollup"]

    # Every group/finding shape the SKILL.md input contract promises must round-trip through JSON.
    group = data["groups"][0]
    for group_key in ("id", "name", "type_slug", "findings"):
        assert group_key in group
    assert group["findings"], "demo engagement's first group should have seeded findings"
    finding = group["findings"][0]
    for finding_key in ("id", "title", "severity", "blocks_html", "artifacts"):
        assert finding_key in finding

    json.dumps(data)  # must be plain-JSON-serializable, as the skill's input contract requires


def test_context_sidecar_lookup_by_name(tmp_path):
    db_path = tmp_path / "fraction.db"
    _seed_demo_db(db_path)

    sidecar = _load_sidecar_module()
    with sidecar.open_readonly_session(db_path) as session:
        data = sidecar.build_sidecar_dict(session, engagement_name="Acme Q3 Assessment")

    assert data["engagement_name"] == "Acme Q3 Assessment"


def test_context_sidecar_missing_engagement_raises(tmp_path):
    db_path = tmp_path / "fraction.db"
    _seed_demo_db(db_path)

    sidecar = _load_sidecar_module()
    with sidecar.open_readonly_session(db_path) as session:
        with pytest.raises(LookupError):
            sidecar.build_sidecar_dict(session, engagement_id=999_999)


def test_context_sidecar_session_is_genuinely_readonly(tmp_path):
    """The helper must be read-only at the connection level, not merely by convention: a write attempt
    against the session it hands out must fail, even if calling code tried to misuse it."""
    from sqlalchemy.exc import OperationalError

    db_path = tmp_path / "fraction.db"
    _seed_demo_db(db_path)

    sidecar = _load_sidecar_module()
    with sidecar.open_readonly_session(db_path) as session:
        # A plain read still works over this "read-only" session...
        assert session.execute(sidecar.select(sidecar.Engagement)).first() is not None
        # ...but any attempted write is rejected by SQLite itself (mode=ro), not merely by convention.
        session.add(sidecar.Engagement(name="should never be persisted"))
        with pytest.raises(OperationalError, match="readonly"):
            session.commit()


def test_context_sidecar_handles_path_with_uri_reserved_characters(tmp_path):
    """A ``--db`` path containing characters SQLite's URI parser treats specially (space, ``#``) must
    still resolve to the real file and stay read-only — regression check for the percent-encoding fix in
    ``_readonly_engine`` (an unencoded ``#`` would truncate the path / silently drop ``mode=ro``)."""
    tricky_dir = tmp_path / "has space & # hash"
    tricky_dir.mkdir()
    db_path = tricky_dir / "fraction.db"
    engagement_id = _seed_demo_db(db_path)

    sidecar = _load_sidecar_module()
    with sidecar.open_readonly_session(db_path) as session:
        data = sidecar.build_sidecar_dict(session, engagement_id=engagement_id)
        assert data["engagement_id"] == engagement_id

        from sqlalchemy.exc import OperationalError

        session.add(sidecar.Engagement(name="should never be persisted"))
        with pytest.raises(OperationalError, match="readonly"):
            session.commit()


def test_context_sidecar_cli_writes_json_file(tmp_path):
    db_path = tmp_path / "fraction.db"
    engagement_id = _seed_demo_db(db_path)
    out_path = tmp_path / "report.context.json"

    sidecar = _load_sidecar_module()
    rc = sidecar.main(
        ["--db", str(db_path), "--engagement-id", str(engagement_id), "--out", str(out_path)]
    )

    assert rc == 0
    assert out_path.is_file()
    data = json.loads(out_path.read_text(encoding="utf-8"))
    assert data["engagement_id"] == engagement_id
    assert {"groups", "rollup", "variables"} <= data.keys()

    # The DB itself must be untouched by the whole CLI round-trip.
    raw = sqlite3.connect(db_path)
    try:
        (count,) = raw.execute("SELECT COUNT(*) FROM fraction_engagements").fetchone()
        assert count == 1
    finally:
        raw.close()
