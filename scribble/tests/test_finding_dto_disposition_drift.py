"""Drift guard: every ``FindingDTO`` field carries an explicit disposition (map #616 / #617).

``EngagementFinding`` is a LOSSLESS SUPERSET of the scan ``FindingDTO``. ``scribble.dispositions``
declares exactly one considered disposition per DTO field (typed column / snapshot-only / reasoned drop).
This guard fails when a field appears with no disposition, or a disposition names a field the DTO no
longer has — so a future DTO change forces a deliberate decision instead of silently losing data.

Pattern: lotek core's ``tests/test_runner_reachability_single_source.py`` (a registry completeness sweep
with an explicit, reasoned set). ``source_facts`` captures the whole DTO verbatim, so the guard mainly
polices that a NEW field gets a considered home.
"""

from __future__ import annotations

import dataclasses
import json
import uuid

import pytest

from scribble.dispositions import (
    DISPOSITIONS,
    EDITABLE_COLUMNS,
    FINDING_DTO_FIELDS,
    Home,
    confidence_from_dto,
    snapshot_source_facts,
    status_from_dto,
)
from scribble.enums import Confidence, FindingStatus
from tests.conftest import FakeFindingDTO


def test_no_duplicate_dispositions():
    fields = [d.field for d in DISPOSITIONS]
    assert len(fields) == len(set(fields)), f"duplicate disposition field(s): {fields}"


def test_home_column_consistency():
    """A COLUMN disposition names its EngagementFinding column; a SOURCE_FACTS/DROP one names none."""
    for d in DISPOSITIONS:
        if d.home is Home.column:
            assert d.column, f"{d.field}: home=column but no column named"
        else:
            assert d.column is None, f"{d.field}: home={d.home} must not name a typed column"


def test_editable_columns_are_the_editable_typed_columns():
    expected = {d.column for d in DISPOSITIONS
                if d.home is Home.column and d.operator.value == "editable" and d.column}
    assert EDITABLE_COLUMNS == expected


def test_registry_covers_exactly_the_fake_dto_fields():
    """FakeFindingDTO is scribble's own mirror of ``host_contract.FindingDTO`` (used by every promote
    test). Keeping the registry == the fake's fields is the ALWAYS-ON drift catch: adding a field to the
    fake (mirroring a real DTO change) without a disposition fails HERE, with no lotek dependency."""
    fake_fields = {f.name for f in dataclasses.fields(FakeFindingDTO)}
    assert FINDING_DTO_FIELDS == fake_fields, (
        f"disposition drift vs FakeFindingDTO: missing={fake_fields - FINDING_DTO_FIELDS} "
        f"extra={FINDING_DTO_FIELDS - fake_fields}"
    )


def test_registry_matches_the_real_finding_dto_when_available():
    """The REAL drift catch against the canonical boundary: when lotek's ``app.host_contract`` is
    importable (mounted in lotek, or a lotek checkout on the path), assert the registry covers exactly
    the real ``FindingDTO``. Honest SKIP otherwise — scribble's own venv does not depend on lotek (the
    boundary is duck-typed); this coupling is proven MOUNTED in lotek core's suite. Never ``sys.path``
    -insert lotek here — ``importorskip`` is the sanctioned probe."""
    host_contract = pytest.importorskip("app.host_contract")
    real_fields = {f.name for f in dataclasses.fields(host_contract.FindingDTO)}
    assert FINDING_DTO_FIELDS == real_fields, (
        f"disposition drift vs host_contract.FindingDTO: missing={real_fields - FINDING_DTO_FIELDS} "
        f"extra={FINDING_DTO_FIELDS - real_fields}"
    )


def test_snapshot_covers_every_registry_field_and_is_json_safe():
    dto = FakeFindingDTO(id=uuid.uuid7(), references=["https://example/1"], facts={"host": "10.0.0.1"})
    snap = snapshot_source_facts(dto)
    assert set(snap) == FINDING_DTO_FIELDS  # every field present -> losslessness is structural
    assert snap["id"] == str(dto.id)  # a uuid id is coerced to str (JSON has no UUID type)
    json.dumps(snap)  # the JSON column serializes this on commit; it must never raise


def test_enum_mapping_is_defensive():
    assert confidence_from_dto(FakeFindingDTO(id=1, confidence="high")) is Confidence.high
    assert status_from_dto(FakeFindingDTO(id=1, status="fixed")) is FindingStatus.fixed
    # an unknown/absent value degrades to the default rather than raising (harness no kinder than prod)
    assert confidence_from_dto(FakeFindingDTO(id=1, confidence="bogus")) is Confidence.medium
    assert status_from_dto(FakeFindingDTO(id=1, status="bogus")) is FindingStatus.new
