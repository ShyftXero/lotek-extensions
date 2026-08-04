"""`scribble/facts.py` — the ONE shape rule table (CONTRACT-FACTS §4.2), one case per shape for BOTH
`resolve_variables` (per-finding) and `synthesize_parent_variables` (parent aggregation over N
children). Generic BY VALUE SHAPE only: no tool name, no `dto.source` branch anywhere in
`scribble/facts.py`, and — per CONTRACT-FACTS §7.3 — no tool name in THIS FILE either (asserted below
by a literal grep over its own source).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from scribble.facts import resolve_variables, synthesize_parent_variables


@dataclass
class _Dto:
    """A minimal `FindingDTO`-shaped stand-in: only `facts` + the allowlisted plain fields
    `scribble.facts._ALLOWED_FIELDS` may reference via `{"field": "..."}`."""

    facts: dict = field(default_factory=dict)
    target_host: str | None = None
    severity: str | None = None


def _decl(key: str, rules: list[dict]) -> list[tuple[str, str | None, list]]:
    return [(key, None, rules)]


# ── per-finding (resolve_variables) ─────────────────────────────────────────────────────────────


def test_shape_scalar_renders_the_value_verbatim():
    dto = _Dto(facts={"x": "corp.example"})
    out = resolve_variables(dto, _decl("K", [{"fact": "x", "shape": "scalar"}]))
    assert out == {"K": "corp.example"}


def test_shape_scalar_applies_optional_template_wrapper():
    dto = _Dto(facts={"x": "q"})
    rule = {"fact": "x", "shape": "scalar", "template": "param '{value}'"}
    out = resolve_variables(dto, _decl("K", [rule]))
    assert out == {"K": "param 'q'"}


def test_shape_list_joins_sorted_deduped():
    dto = _Dto(facts={"x": ["bob", "alice", "bob"]})
    out = resolve_variables(dto, _decl("K", [{"fact": "x", "shape": "list"}]))
    assert out == {"K": "alice, bob"}


def test_shape_list_accepts_comma_separated_string():
    dto = _Dto(facts={"x": "bob, alice ,bob"})
    out = resolve_variables(dto, _decl("K", [{"fact": "x", "shape": "list"}]))
    assert out == {"K": "alice, bob"}


def test_shape_count_over_a_list_is_its_length():
    dto = _Dto(facts={"x": ["a", "b", "c"]})
    out = resolve_variables(dto, _decl("K", [{"fact": "x", "shape": "count"}]))
    assert out == {"K": "3"}


def test_shape_count_over_an_int_is_the_int_itself():
    dto = _Dto(facts={"x": 42})
    out = resolve_variables(dto, _decl("K", [{"fact": "x", "shape": "count"}]))
    assert out == {"K": "42"}


def test_shape_host_extracts_bare_host_from_a_url():
    dto = _Dto(facts={"x": "https://h.example/path?y=1"})
    out = resolve_variables(dto, _decl("K", [{"fact": "x", "shape": "host"}]))
    assert out == {"K": "h.example"}


def test_shape_host_strips_a_trailing_port():
    dto = _Dto(facts={"x": "10.0.0.5:22"})
    out = resolve_variables(dto, _decl("K", [{"fact": "x", "shape": "host"}]))
    assert out == {"K": "10.0.0.5"}


def test_shape_severity_renders_verbatim():
    dto = _Dto(severity="high")
    out = resolve_variables(dto, _decl("K", [{"field": "severity", "shape": "severity"}]))
    assert out == {"K": "high"}


def test_shape_parent_domain_of_a_multi_label_host():
    dto = _Dto(facts={"x": "dc01.cheddarsale.local"})
    out = resolve_variables(dto, _decl("K", [{"fact": "x", "shape": "parent_domain"}]))
    assert out == {"K": "cheddarsale.local"}


def test_shape_parent_domain_is_empty_for_an_ip():
    dto = _Dto(facts={"x": "10.0.0.5"})
    out = resolve_variables(dto, _decl("K", [{"fact": "x", "shape": "parent_domain"}]))
    assert out == {"K": ""}


def test_shape_parent_domain_is_empty_for_a_two_label_name():
    dto = _Dto(facts={"x": "example.com"})
    out = resolve_variables(dto, _decl("K", [{"fact": "x", "shape": "parent_domain"}]))
    assert out == {"K": ""}


def test_absent_or_unknown_shape_degrades_to_scalar():
    dto = _Dto(facts={"x": "verbatim value"})
    out = resolve_variables(dto, _decl("K", [{"fact": "x", "shape": "not-a-real-shape"}]))
    assert out == {"K": "verbatim value"}


def test_no_rule_resolves_to_empty_string_not_omitted():
    dto = _Dto(facts={})
    out = resolve_variables(dto, _decl("K", [{"fact": "missing", "shape": "scalar"}]))
    assert out == {"K": ""}


def test_first_resolving_rule_wins_over_later_candidates():
    dto = _Dto(facts={"b": "second"})
    out = resolve_variables(
        dto, _decl("K", [{"fact": "a", "shape": "scalar"}, {"fact": "b", "shape": "scalar"}])
    )
    assert out == {"K": "second"}


# ── parent synthesis (synthesize_parent_variables) ──────────────────────────────────────────────


def test_parent_scalar_unanimous_children_agree():
    children = [_Dto(facts={"x": "corp.example"}), _Dto(facts={"x": "corp.example"})]
    out = synthesize_parent_variables(children, _decl("K", [{"fact": "x", "shape": "scalar"}]))
    assert out == {"K": "corp.example"}


def test_parent_scalar_two_distinct_values_never_guesses_yields_empty():
    """THE two-distinct-values rule (CONTRACT-FACTS §4.2): a parent must never silently pick one of
    two disagreeing children's values."""
    children = [_Dto(facts={"x": "corp-a.example"}), _Dto(facts={"x": "corp-b.example"})]
    out = synthesize_parent_variables(children, _decl("K", [{"fact": "x", "shape": "scalar"}]))
    assert out == {"K": ""}


def test_parent_parent_domain_two_distinct_domains_yields_empty():
    children = [
        _Dto(facts={"x": "dc01.corp-a.example"}),
        _Dto(facts={"x": "dc02.corp-b.example"}),
    ]
    out = synthesize_parent_variables(children, _decl("K", [{"fact": "x", "shape": "parent_domain"}]))
    assert out == {"K": ""}


def test_parent_parent_domain_unanimous_children_agree():
    children = [_Dto(facts={"x": "dc01.corp.example"}), _Dto(facts={"x": "dc02.corp.example"})]
    out = synthesize_parent_variables(children, _decl("K", [{"fact": "x", "shape": "parent_domain"}]))
    assert out == {"K": "corp.example"}


def test_parent_list_unions_and_sorts_children():
    children = [_Dto(facts={"x": ["bob"]}), _Dto(facts={"x": ["alice", "bob"]})]
    out = synthesize_parent_variables(children, _decl("K", [{"fact": "x", "shape": "list"}]))
    assert out == {"K": "alice, bob"}


def test_parent_list_caps_at_40_items_with_overflow_note():
    items = [f"user{i:03d}" for i in range(45)]
    children = [_Dto(facts={"x": items})]
    out = synthesize_parent_variables(children, _decl("K", [{"fact": "x", "shape": "list"}]))
    value = out["K"]
    assert value.endswith("…and 5 more")
    shown = value.split(" …and ")[0]
    assert len(shown.split(", ")) == 40


def test_parent_count_sums_children():
    children = [_Dto(facts={"x": ["a", "b"]}), _Dto(facts={"x": 3})]
    out = synthesize_parent_variables(children, _decl("K", [{"fact": "x", "shape": "count"}]))
    assert out == {"K": "5"}


def test_parent_host_sorted_distinct_join():
    children = [_Dto(facts={"x": "10.0.0.2"}), _Dto(facts={"x": "10.0.0.1"}), _Dto(facts={"x": "10.0.0.1"})]
    out = synthesize_parent_variables(children, _decl("K", [{"fact": "x", "shape": "host"}]))
    assert out == {"K": "10.0.0.1, 10.0.0.2"}


def test_parent_severity_picks_the_worst_child_severity():
    children = [_Dto(severity="low"), _Dto(severity="critical"), _Dto(severity="medium")]
    out = synthesize_parent_variables(children, _decl("K", [{"field": "severity", "shape": "severity"}]))
    assert out == {"K": "critical"}


def test_parent_severity_tie_resolves_deterministically():
    children = [_Dto(severity="high"), _Dto(severity="high")]
    out = synthesize_parent_variables(children, _decl("K", [{"field": "severity", "shape": "severity"}]))
    assert out == {"K": "high"}


def test_parent_no_child_has_the_fact_yields_empty_not_omitted():
    children = [_Dto(facts={}), _Dto(facts={})]
    out = synthesize_parent_variables(children, _decl("K", [{"fact": "missing", "shape": "count"}]))
    assert out == {"K": ""}


# ── CONTRACT-FACTS §7.3: this file names no tool/source ─────────────────────────────────────────

_DENYLIST = (
    "nuclei", "dalfox", "kerberoast", "asreproast", "certipy", "secretsdump", "responder",
    "netexec", "nxc", "cme", "brutus", "bloodhound", "azurehound", "roadrecon", "enum4linux",
    "kubescape", "autorecon", "nmap", "masscan", "httpx", "subfinder",
)


def test_this_file_names_no_tool():
    source = Path(__file__).read_text()
    # Strip this test's own denylist tuple (its literal string values are the vocabulary being
    # asserted ABOUT, not a violation of it) before scanning the rest of the file.
    source = re.sub(r"_DENYLIST\s*=\s*\([^)]*\)", "", source, flags=re.DOTALL)
    source = source.lower()
    hits = [name for name in _DENYLIST if name in source]
    assert hits == [], f"test_facts_shapes.py names a tool: {hits}"
