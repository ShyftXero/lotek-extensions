"""The Report Boards list (`/scribble/engagements`) is a shared datatable, not a bespoke table.

ext#381: the list reuses the attribute contract ext#380 brought to the sshim relay list (core's
`static/data-table.js`, loaded by lotek's `base.html`) instead of hand-rolling sort/filter/paging.
Client-side filter is `data-dt-colfilter`, sortable columns are `th[data-sort]`, and per-page paging is
`data-dt-page-size`. Standalone scribble ships no `data-table.js` at all, so the SAME markup must still
be a plain, fully server-rendered table: the component is additive, never a rendering dependency.

Non-vacuity. A test that only asserted "a <table> exists" would pass on the plain table this replaced.
`test_the_list_carries_the_data_table_contract` fails on it (an unwired table has no `data-dt`, no
`data-dt-colfilter`, no `data-dt-page-size`, and no `th[data-sort]`), and it parses the actual tag
attributes rather than substring-matching "data-dt", so `data-dt-page-size` alone cannot satisfy it.
`test_the_list_degrades_to_a_plain_table_without_the_widget` is the other half of the #380 parity
contract: it proves the rows are in the initial HTML and the page references no widget asset, i.e. the
absence of the host component degrades to a readable table instead of an empty one.
"""

from __future__ import annotations

import re
import uuid
from pathlib import Path

UI = "/scribble"

_TEMPLATE = (
    Path(__file__).resolve().parents[1] / "scribble" / "templates" / "scribble" / "engagements.html"
)

_TABLE_TAG = re.compile(r"<table\b[^>]*\bid=\"scribble-engagements-table\"[^>]*>", re.S)


def _summary(core_id, name, client_id, client_name) -> dict:
    return {"id": core_id, "name": name, "client_id": client_id, "client_name": client_name}


def _attrs(tag: str) -> dict[str, str | None]:
    """Attribute-name -> value for one start tag. Valueless attributes map to None, so `data-dt` can be
    asserted as itself rather than matching inside `data-dt-page-size`."""
    inner = re.sub(r"^<[\w-]+", "", tag).rstrip(">").strip()
    return {
        name: value for name, value in re.findall(r"([:\w-]+)(?:\s*=\s*\"([^\"]*)\")?", inner)
    }


def _table_tag(body: str) -> str:
    match = _TABLE_TAG.search(body)
    assert match, "the Report Boards list must render the shared table (id=scribble-engagements-table)"
    return match.group(0)


def _rows(stub_host, count: int) -> list[str]:
    names = [f"Engagement {i:02d}" for i in range(count)]
    stub_host.engagement_summaries_value = [
        _summary(uuid.uuid7(), name, uuid.uuid7(), f"Client {i:02d}") for i, name in enumerate(names)
    ]
    return names


# ── the wiring: the attribute contract core's data-table.js reads ──────────────────────────────────────


def test_the_list_carries_the_data_table_contract(client, stub_host):
    _rows(stub_host, 1)

    resp = client.get(f"{UI}/engagements")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    attrs = _attrs(_table_tag(body))

    # The table itself opts in, keeps plain-table styling for the standalone fallback, and declares
    # the same paging / per-column filter / empty-state contract as the ext#380 relay list.
    assert "table" in (attrs.get("class") or "").split()
    assert "data-dt" in attrs
    assert attrs["data-dt-page-size"] == "25"          # per-page paging is ON, at 25 rows
    assert "data-dt-colfilter" in attrs                # client-side per-column filter inputs
    assert attrs["data-dt-empty"] == "No report boards."

    # Sortable columns: Engagement, Client, Report board. The actions column carries no data-sort, so
    # the widget leaves it unorderable (the contract's "no data-sort -> not orderable" rule).
    thead = body.split("<thead>", 1)[1].split("</thead>", 1)[0]
    headers = re.findall(r"<th\b[^>]*>(.*?)</th>", thead, re.S)
    assert len(headers) == 4
    sortable = ["data-sort" in _attrs(th) for th in re.findall(r"<th\b[^>]*>", thead)]
    assert sortable == [True, True, True, False]
    labels = [re.sub(r"<[^>]+>", "", h).strip() for h in headers]
    assert labels[:3] == ["Engagement", "Client", "Report board"]


def test_every_row_reaches_the_widget_in_the_initial_html(client, stub_host):
    """The widget snapshots `tbody` at init, so the rows have to be server-rendered, all of them: a
    later page must not depend on JS to have data to sort/filter/page."""
    names = _rows(stub_host, 30)

    body = client.get(f"{UI}/engagements").get_data(as_text=True)
    tbody = body.split("<tbody>", 1)[1].split("</tbody>", 1)[0]
    assert tbody.count("<tr") == 30
    assert all(name in tbody for name in names)
    # 30 rows > the declared page size: paging is a client view over the full set, nothing dropped.
    assert len(names) > 25


# ── the fallback: no host component, still a plain table ───────────────────────────────────────────────


def test_the_list_degrades_to_a_plain_table_without_the_widget(client, stub_host):
    """Standalone scribble has no `data-table.js` route. The page must still be a complete, readable
    table: every row in the markup, no widget asset referenced, so nothing blank to degrade from."""
    names = _rows(stub_host, 3)

    assert client.get("/static/data-table.js").status_code == 404  # the component's asset route is absent

    resp = client.get(f"{UI}/engagements")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)

    assert "data-table.js" not in body  # nothing on the page loads the widget standalone
    assert "dataTables" not in body and "jquery" not in body.lower()
    tbody = body.split("<tbody>", 1)[1].split("</tbody>", 1)[0]
    assert tbody.count("<tr") == 3 and all(name in tbody for name in names)
    assert "table-scroll" in body  # the plain-table wrapper still renders


def test_the_template_redefines_no_core_data_table_or_dt_css():
    """No core CSS redefined (the #380 rule): scribble may add its own page classes but must not ship a
    `.data-table` / `.dt-*` rule that would collide with the host's stylesheet when mounted."""
    source = _TEMPLATE.read_text()
    styles = "".join(re.findall(r"<style>(.*?)</style>", source, re.S))
    assert styles, "expected the page's own <style> block"
    assert ".dt-" not in styles and ".data-table" not in styles
    assert "_data_table.html" not in source  # no host template seam: renders standalone too
