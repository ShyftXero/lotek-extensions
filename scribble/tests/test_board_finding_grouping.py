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


def _stub_bucket(**kw):
    """A rollup bucket the pivot template can render. `items` default to two per-host findings."""
    from types import SimpleNamespace

    kw.setdefault("severity", "critical")
    kw.setdefault("cves", ["CVE-2017-0144"])
    if "items" not in kw:
        hosts = kw.get("hosts") or ["10.0.0.1", "10.0.0.2"]
        kw["items"] = [SimpleNamespace(id=str(uuid.uuid7()), host=h,
                                       payload={"f": SimpleNamespace(title=kw.get("label", "V"))})
                       for h in hosts]
    kw.setdefault("hosts", sorted({it.host for it in kw["items"]}))
    kw.setdefault("count", len(kw["items"]))
    kw.setdefault("key", kw["items"][0].host if kw["items"] else "")
    kw.setdefault("label", "EternalBlue")
    return SimpleNamespace(**kw)


def test_board_renders_the_group_by_tabs_and_a_rollup(client, stub_host, session_factory):
    eng_id = _engagement_with_findings(session_factory)
    stub_host.group_findings_value = [_stub_bucket(label="EternalBlue", hosts=["10.0.0.1", "10.0.0.2"])]
    html = client.get(f"{UI}/engagements/{eng_id}").get_data(as_text=True)
    assert 'role="tablist"' in html
    assert 'data-view="kind"' in html and 'data-view="host"' in html
    # Q1: the axis is named, and the tabs are Section / Vulnerability / Host (not the old "Board" / "By …").
    assert "Group by:" in html
    assert ">Section<" in html and ">Vulnerability<" in html and ">Host<" in html
    assert "By vulnerability" not in html and ">Board<" not in html
    assert "scribble-kind-table" in html
    assert "https://nvd.nist.gov/vuln/detail/CVE-2017-0144" in html


def test_rollup_hosts_are_links_and_findings_column_is_dropped(client, stub_host, session_factory):
    """Q3/Q4: affected hosts ARE the finding links; the redundant per-host 'Findings' column is gone."""
    from types import SimpleNamespace

    eng_id = _engagement_with_findings(session_factory)
    fid = str(uuid.uuid7())
    it = SimpleNamespace(id=fid, host="10.9.9.9", payload={"f": SimpleNamespace(title="EternalBlue")})
    stub_host.group_findings_value = [_stub_bucket(label="EternalBlue", items=[it])]
    html = client.get(f"{UI}/engagements/{eng_id}").get_data(as_text=True)
    kind = html.split('id="scribble-kind-table"', 1)[1].split("</table>", 1)[0]
    # Column set: Affected hosts + Assign present; Findings column removed.
    assert ">Affected hosts<" in kind and ">Assign<" in kind
    assert ">Findings<" not in kind
    # The host itself is a link to ITS finding.
    assert f"/findings/{fid}" in kind
    assert ">10.9.9.9<" in kind


def test_rollup_see_more_collapses_long_host_lists(client, stub_host, session_factory):
    """Q3: beyond the threshold, extra hosts are pre-rendered but hidden behind a '+N more…' reveal."""
    eng_id = _engagement_with_findings(session_factory)
    hosts = [f"10.0.0.{i}" for i in range(1, 13)]  # 12 > the 8 threshold
    stub_host.group_findings_value = [_stub_bucket(label="LLMNR", hosts=hosts)]
    html = client.get(f"{UI}/engagements/{eng_id}").get_data(as_text=True)
    kind = html.split('id="scribble-kind-table"', 1)[1].split("</table>", 1)[0]
    assert "rl-overflow" in kind          # the hidden extra hosts exist in the DOM (DataTables-searchable)
    assert 'class="btn-link rl-more"' in kind
    assert "more…" in kind


def test_rollup_assign_control_carries_the_buckets_finding_ids(client, stub_host, session_factory):
    """Q2: each bucket can bulk-assign ALL its findings to a report section (cross-axis move)."""
    from types import SimpleNamespace

    eng_id = _engagement_with_findings(session_factory)
    a, b_ = str(uuid.uuid7()), str(uuid.uuid7())
    items = [SimpleNamespace(id=a, host="10.0.0.1", payload={"f": SimpleNamespace(title="X")}),
             SimpleNamespace(id=b_, host="10.0.0.2", payload={"f": SimpleNamespace(title="X")})]
    stub_host.group_findings_value = [_stub_bucket(label="X", items=items)]
    html = client.get(f"{UI}/engagements/{eng_id}").get_data(as_text=True)
    assert "rl-assign-btn" in html and "rl-assign-sel" in html
    assert f'data-finding-ids="{a},{b_}"' in html
    assert ">No section</option>" in html   # the null-section target


def test_rollup_multi_finding_host_shows_the_plus_edge(client, stub_host, session_factory):
    """Q4 edge: a host carrying two findings of one vuln links the first + a '·+N' to the next, so
    neither finding is dropped from the affected-hosts list."""
    from types import SimpleNamespace

    eng_id = _engagement_with_findings(session_factory)
    a, b_ = str(uuid.uuid7()), str(uuid.uuid7())
    items = [SimpleNamespace(id=a, host="10.0.0.7", payload={"f": SimpleNamespace(title="Dup")}),
             SimpleNamespace(id=b_, host="10.0.0.7", payload={"f": SimpleNamespace(title="Dup")})]
    stub_host.group_findings_value = [_stub_bucket(label="Dup", items=items, hosts=["10.0.0.7"])]
    html = client.get(f"{UI}/engagements/{eng_id}").get_data(as_text=True)
    kind = html.split('id="scribble-kind-table"', 1)[1].split("</table>", 1)[0]
    assert 'class="rl-plus"' in kind                       # the ·+N link for the 2nd finding on the host
    assert f"/findings/{a}" in kind and f"/findings/{b_}" in kind  # BOTH findings reachable
    assert "+1" in kind                                    # one extra beyond the first


def test_bulk_bar_lives_inside_the_section_panel(client, stub_host, session_factory):
    """Q4/wedge: the multi-select bulk bar sits INSIDE #scribble-view-board, so it hides in the pivots
    (where its checkboxes don't exist) instead of rendering as an inert control across every tab."""
    eng_id = _engagement_with_findings(session_factory)
    stub_host.group_findings_value = [_stub_bucket()]
    html = client.get(f"{UI}/engagements/{eng_id}").get_data(as_text=True)
    board_open = html.index('id="scribble-view-board"')
    board_close = html.index("<!-- /#scribble-view-board -->")
    bar = html.index('id="scribble-bulk-bar"')
    assert board_open < bar < board_close, "bulk bar must be inside the Section panel"
    assert "data-dt" in html  # the pivot tables opt into DataTables (sort/filter/paginate when mounted)


def test_board_hides_the_toggle_when_no_groupable_findings(client, stub_host, session_factory):
    # Default stub returns [] -> findings_by_kind is empty -> no toggle, board view stands alone.
    eng_id = _engagement_with_findings(session_factory)
    stub_host.group_findings_value = []
    html = client.get(f"{UI}/engagements/{eng_id}").get_data(as_text=True)
    assert 'role="tablist"' not in html
    assert 'data-view="kind"' not in html
