"""Phase 1b (lotek #829): the board's By-vulnerability / By-host toggle.

Scope of THIS module (the scribble half): the adapter that maps a ``BoardFinding`` into the seam's row
shape, the flatten that drops promotion shell-parents, the fail-closed wrapper, and the board route +
template wiring (rows reach the seam; tabs render when buckets come back). The bucketer itself lives in
lotek core and is NOT importable here, so end-to-end COLLAPSE against the real bucketer and the
cross-surface no-drift guarantee are proven MOUNTED in core's ``tests/test_scribble_ui_mounted.py``.
"""

from __future__ import annotations

import uuid

from scribble import finding_grouping_adapter, findings_service, host
from scribble.enums import Severity
from scribble.models import BoardFinding, Client, FindingGroup, ReportBoard

UI = "/scribble"


def _finding(**kw) -> BoardFinding:
    kw.setdefault("id", uuid.uuid7())
    kw.setdefault("title", "Some finding")
    kw.setdefault("severity", Severity.medium)
    return BoardFinding(**kw)


# --------------------------------------------------------------------------- adapter (pure)

def test_adapter_maps_fields_from_a_promoted_finding():
    f = _finding(
        title="MS17-010 EternalBlue",
        severity=Severity.critical,
        target_host="10.0.0.5",
        cve_ids=["CVE-2017-0144"],
        source_facts={"dedupe_key": "nuclei:ms17-010", "source": "nuclei"},
    )
    row = finding_grouping_adapter.board_finding_to_group_row(f)
    assert row["id"] == str(f.id)
    assert row["host"] == "10.0.0.5"
    assert row["cves"] == ("CVE-2017-0144",)  # passthrough, already normalized — not a str, not re-parsed
    assert row["severity"] == "critical"
    assert row["kind_key"] == "nuclei:ms17-010"  # the snapshotted dedupe_key wins
    assert row["label"] == "MS17-010 EternalBlue"
    assert row["payload"] == {"f": f}


def test_adapter_kind_key_falls_back_to_source_title_without_a_dedupe_key():
    # A manually-authored finding (no source_facts) or one whose DTO had no dedupe_key still groups.
    f = _finding(title="LLMNR poisoning", source_facts={"source": "nmap"})
    assert finding_grouping_adapter.board_finding_to_group_row(f)["kind_key"] == "nmap:LLMNR poisoning"
    bare = _finding(title="Hand-written", source_facts=None)
    assert finding_grouping_adapter.board_finding_to_group_row(bare)["kind_key"] == ":Hand-written"


def test_same_vuln_across_hosts_shares_one_kind_key():
    # The basis of the collapse: same dedupe_key on every per-host instance -> one by-vulnerability row.
    facts = {"dedupe_key": "nuclei:ms17-010", "source": "nuclei"}
    keys = {
        finding_grouping_adapter.board_finding_to_group_row(
            _finding(title="EternalBlue", target_host=h, source_facts=facts)
        )["kind_key"]
        for h in ("10.0.0.1", "10.0.0.2", "10.0.0.3")
    }
    assert keys == {"nuclei:ms17-010"}


# --------------------------------------------------------------------------- flatten (nesting-aware)

def test_flatten_drops_shell_parents_keeps_children_and_leaves():
    parent = _finding(title="EternalBlue (aggregate)", target_host=None)
    child_a = _finding(title="EternalBlue", target_host="10.0.0.1", parent_id=parent.id)
    child_b = _finding(title="EternalBlue", target_host="10.0.0.2", parent_id=parent.id)
    leaf = _finding(title="Self-signed cert", target_host="10.0.0.9")  # no children

    flat = findings_service.flatten_for_grouping([parent, child_a, child_b, leaf])
    ids = {f.id for f in flat}
    assert parent.id not in ids, "the aggregation shell parent must be dropped"
    assert ids == {child_a.id, child_b.id, leaf.id}


# --------------------------------------------------------------------------- wrapper (fail-closed)

def test_group_findings_wrapper_returns_empty_when_unmounted():
    # No app context -> get_config() raises -> host_hook is None -> the board's tabs hide.
    assert host.group_findings([{"id": "1"}], by="kind") == []


# --------------------------------------------------------------------------- route + template wiring

def _engagement_with_findings(session_factory) -> uuid.UUID:
    with session_factory() as db:
        client = Client(name="Acme")
        db.add(client)
        db.flush()
        eng = ReportBoard(name="Q3", client_id=client.id, company_name="Acme")
        grp = FindingGroup(engagement=eng, name="Internal", order_index=0)
        db.add_all([eng, grp])
        db.flush()
        db.add_all([
            _finding(engagement_id=eng.id, group_id=grp.id, title="EternalBlue",
                     severity=Severity.critical, target_host="10.0.0.1", cve_ids=["CVE-2017-0144"],
                     source_facts={"dedupe_key": "nuclei:ms17-010", "source": "nuclei"}),
            _finding(engagement_id=eng.id, group_id=grp.id, title="EternalBlue",
                     severity=Severity.critical, target_host="10.0.0.2", cve_ids=["CVE-2017-0144"],
                     source_facts={"dedupe_key": "nuclei:ms17-010", "source": "nuclei"}),
        ])
        db.commit()
        return eng.id


def test_board_passes_adapted_rows_to_the_seam(client, stub_host, session_factory):
    eng_id = _engagement_with_findings(session_factory)
    stub_host.group_findings_calls.clear()
    client.get(f"{UI}/engagements/{eng_id}")

    calls = stub_host.group_findings_calls
    assert [by for _, by in calls] == ["kind", "host"], "route asks the seam for both pivots"
    rows, _ = calls[0]
    assert len(rows) == 2
    assert {r["kind_key"] for r in rows} == {"nuclei:ms17-010"}  # both EternalBlue rows, one kind
    assert {r["host"] for r in rows} == {"10.0.0.1", "10.0.0.2"}
    assert all(set(r) == {"id", "host", "cves", "severity", "kind_key", "label", "payload"} for r in rows)


def test_board_renders_the_three_tabs_and_a_rollup_when_buckets_come_back(
    client, stub_host, session_factory
):
    from types import SimpleNamespace

    eng_id = _engagement_with_findings(session_factory)
    it = SimpleNamespace(id=str(uuid.uuid7()), host="10.0.0.1",
                         payload={"f": SimpleNamespace(title="EternalBlue")})
    stub_host.group_findings_value = [
        SimpleNamespace(label="EternalBlue", severity="critical", hosts=["10.0.0.1", "10.0.0.2"],
                        cves=["CVE-2017-0144"], count=2, key="EternalBlue", items=[it])
    ]
    html = client.get(f"{UI}/engagements/{eng_id}").get_data(as_text=True)
    # data-view="kind" / role="tablist" appear ONLY in the rendered toggle markup — the bare class name
    # also lives in the always-present <style>/<script>, so it is not a presence signal.
    assert 'role="tablist"' in html
    assert 'data-view="kind"' in html and 'data-view="host"' in html
    assert "By vulnerability" in html and "By host" in html
    assert "scribble-kind-table" in html
    assert "https://nvd.nist.gov/vuln/detail/CVE-2017-0144" in html


def test_board_hides_the_toggle_when_no_groupable_findings(client, stub_host, session_factory):
    # Default stub returns [] -> findings_by_kind is empty -> no toggle, board view stands alone.
    eng_id = _engagement_with_findings(session_factory)
    stub_host.group_findings_value = []
    html = client.get(f"{UI}/engagements/{eng_id}").get_data(as_text=True)
    assert 'role="tablist"' not in html
    assert 'data-view="kind"' not in html
