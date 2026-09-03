"""``lotek_kit.attackpath`` — the ported document model.

The port is verbatim from ``vector/vector/schema.py`` apart from the schema id, so these tests
concentrate on the two things the port CHANGED (the id, and the new read-side predicate) plus the
contract that made the module safe to move: it never raises, whatever it is handed.
"""

from __future__ import annotations

import pytest

from lotek_kit.attackpath import (
    LEGACY_SCHEMA_IDS,
    MAX_NODES,
    SCHEMA_ID,
    SUPPORTED_SCHEMA_IDS,
    blank_model,
    is_supported_schema_id,
    normalize,
)


def test_the_schema_id_has_no_extension_prefix():
    """The ``vector.`` prefix named an extension that map #148 deletes. A shared contract cannot be
    named after one of its consumers."""
    assert SCHEMA_ID == "attackpath/v1"
    assert "vector" not in SCHEMA_ID


def test_the_old_id_is_still_recognised_on_read():
    """Stored documents carry the old id. They must keep loading — the migration is 'it gets read'."""
    assert LEGACY_SCHEMA_IDS == ("vector.attackpath/v1",)
    assert is_supported_schema_id("vector.attackpath/v1")
    assert is_supported_schema_id(SCHEMA_ID)
    assert SUPPORTED_SCHEMA_IDS == (SCHEMA_ID, *LEGACY_SCHEMA_IDS)


@pytest.mark.parametrize("value", ["", "attackpath/v2", "some.other/v1", None, 1, ["attackpath/v1"]])
def test_a_foreign_document_is_not_claimed(value):
    """``normalize`` cannot make this distinction — it accepts anything and stamps its own id over the
    top — so a caller importing an unknown document needs the predicate to refuse first."""
    assert is_supported_schema_id(value) is False


def test_normalizing_a_legacy_document_rewrites_its_id():
    out = normalize({"schema": "vector.attackpath/v1", "meta": {"title": "old"}})
    assert out["schema"] == SCHEMA_ID
    assert out["meta"]["title"] == "old"


def test_a_forged_schema_id_is_overwritten_not_trusted():
    out = normalize({"schema": "totally-made-up", "meta": {"title": "x"}})
    assert out["schema"] == SCHEMA_ID


@pytest.mark.parametrize(
    "hostile",
    [
        None,
        [],
        "a string",
        42,
        {"zones": "not a list"},
        {"nodes": [None, 1, "x"]},
        {"edges": [{"from": "nowhere", "to": "nothing"}]},
        {"phases": {"not": "a list"}},
        {"meta": "not a dict"},
    ],
)
def test_normalize_never_raises(hostile):
    """The contract that made this module safe to share: it degrades, it does not reject. Its output
    is embedded verbatim into an exported deliverable, so a raise here breaks a report render."""
    out = normalize(hostile)
    assert out["schema"] == SCHEMA_ID
    assert isinstance(out["zones"], list)
    assert isinstance(out["nodes"], list)
    assert isinstance(out["edges"], list)


def test_caps_are_enforced_rather_than_trusted():
    out = normalize({"nodes": [{"id": f"n{i}", "label": str(i)} for i in range(MAX_NODES + 50)]})
    assert len(out["nodes"]) <= MAX_NODES


def test_an_edge_to_a_nonexistent_node_is_dropped():
    out = normalize(
        {
            "zones": [{"id": "z", "title": "Z"}],
            "nodes": [{"id": "a", "zone": "z", "label": "A"}],
            "edges": [{"from": "a", "to": "ghost"}],
        }
    )
    assert out["edges"] == []


def test_blank_model_round_trips_through_normalize():
    """``blank_model`` is what a new document starts as; if it were not already canonical, the first
    save would silently rewrite it and every diff after that would be noise."""
    blank = blank_model("Teaching example")
    assert blank == normalize(blank)
    assert blank["meta"]["title"] == "Teaching example"


def test_style_is_an_unvalidated_passthrough():
    """Pinned deliberately, as a WARNING rather than an endorsement: ``style`` is the one key this
    module does not constrain. Anything that must bound what a document can carry cannot lean on
    normalize() to do it — see the caveat in the module docstring."""
    out = normalize({"style": {"anything": ["at", "all"], "nested": {"deep": True}}})
    assert out["style"] == {"anything": ["at", "all"], "nested": {"deep": True}}
