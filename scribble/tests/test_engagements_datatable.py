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
`test_the_contract_test_breaks_on_the_plain_table_this_replaced` derives that plain markup from the real
page and re-runs the same predicates on it, so the claim is proven rather than asserted.

Two halves of the mount contract, both failure modes the widget cannot police itself:

* CSS. `data-table.js` fails QUIET (an early `return` when the libs are absent, a `console.warn` inside
  its init `try/catch`), so a bad stylesheet or a broken init precondition leaves a readable page that
  is simply not the widget. `test_no_shipped_stylesheet_collides_with_the_host_data_table` scans EVERY
  stylesheet scribble ships under `static/`, not just the template's `<style>`: mounted, the extension's
  static tree is on the host page, so a `.data-table` / `.dt-*` / `table.dataTable` rule added to
  `scribble.css` restyles the host's own tables on every scribble route while a template-only guard
  stays green.
* Degradation. `test_the_list_degrades_to_a_plain_table_without_the_widget` proves the rows are in the
  initial HTML, the page references no widget asset and renders no widget chrome, i.e. the absence of the
  host component leaves a readable table instead of an empty one.

RESIDUAL, stated plainly: no JavaScript runs in this file. Playwright is an optional dev dependency and
the suite's browser e2e tests skip cleanly when no browser binary is present, and this environment does
not run a headless Chromium for route tests, so nothing here can observe the DOM DataTables leaves
behind (`.dt-container`, the `dataTable` class, its pager). What is pinned instead is everything the init
path reads before it can produce that DOM: the selector `initAll()` uses (`table[data-dt]`), the
structural gates `init()` bails on (`if (!head || !head.rows.length) return`, a `tbody` to snapshot), and
the attributes it parses. A rejection caused by a broken precondition is caught here; a rejection from
CSP, a missing vendored lib, or an exception elsewhere on the host page is out of reach of a
non-browser test and stays open.
"""

from __future__ import annotations

import re
import uuid
from pathlib import Path

import pytest

UI = "/scribble"

_PKG = Path(__file__).resolve().parents[1] / "scribble"
_TEMPLATE = _PKG / "templates" / "scribble" / "engagements.html"
_STATIC = _PKG / "static"

_TABLE_TAG = re.compile(r"<table\b[^>]*\bid=\"scribble-engagements-table\"[^>]*>", re.S)

# What would restyle a host datatable, whichever spelling it uses: the hyphenated `.data-table` the host
# also puts on the element, the camelCase `.dataTable` class DataTables applies, the `.dt-*` widget
# chrome, and an attribute selector on the opt-in hook (which hits every host table, not just ours).
_HOST_SELECTOR = re.compile(r"\.data-table\b|\.dt-[A-Za-z_-]|\bdataTable\b|\[data-dt\b")
_CSS_COMMENT = re.compile(r"/\*.*?\*/", re.S)

# `data-table.js`'s initAll() selector: `querySelectorAll("table[data-dt]")`.
_DT_TABLE = re.compile(r"<table\b[^>]*\bdata-dt(?=[\s>/])[^>]*>", re.S)

# The pre-#381 markup: same table, no opt-in hooks. Used to re-run the contract predicates on a table the
# widget would ignore.
_PLAIN_HOOKS = re.compile(r"""\s+data-dt(?=[\s>/])|\s+data-dt-[\w-]+(?:="[^"]*")?""")


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


def _collisions(css: str) -> list[str]:
    """The host-colliding selectors in one stylesheet body, with comments stripped: a comment cannot
    restyle anything, so it must not be able to trip the guard (and must not be able to hide a rule)."""
    return _HOST_SELECTOR.findall(_CSS_COMMENT.sub("", css))


def _stylesheets() -> dict[Path, str]:
    """Every stylesheet scribble ships under `static/`, keyed by path. All of them, because the mount
    links the extension's static tree into the host page: `scribble.css` from `base.html` on every route,
    anything a page template adds through its `head_extra` block, and anything added later."""
    files = sorted(_STATIC.rglob("*.css"))
    assert files, "expected scribble to ship at least one stylesheet"
    return {path: path.read_text(encoding="utf-8") for path in files}


def _widget_eligible(body: str) -> bool:
    """Would core's `static/data-table.js` pick this page's table up and initialize it?

    A Python model of the gates `initAll()`/`init()` apply, in the order the JS applies them: exactly one
    `table[data-dt]`; a `<thead>` whose first row has cells (`init` returns early without one); and a
    `<tbody>` with rows to snapshot, since DataTables copies the body once at init and never re-reads it.
    Modelling the gates is the closest a non-browser test gets to the widget, so the model itself is
    pinned by `test_the_eligibility_model_rejects_a_table_the_widget_would_skip`."""
    if len(_DT_TABLE.findall(body)) != 1:
        return False
    head = re.search(r"<thead>(.*?)</thead>", body, re.S)
    if not head or not re.search(r"<tr\b[^>]*>\s*<th\b", head.group(1)):
        return False
    tbody = re.search(r"<tbody>(.*?)</tbody>", body, re.S)
    return bool(tbody and re.search(r"<tr\b", tbody.group(1)))


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
    # The widget's message for "rows exist but the filter hides them all". NOT the page's zero-row state,
    # which never reaches the widget at all: `test_no_visible_row_renders_the_plain_empty_state` covers it.
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


def test_the_contract_test_breaks_on_the_plain_table_this_replaced(client, stub_host):
    """Non-vacuity for the contract above, derived from the real page rather than a hand-written string so
    it cannot drift from the template: strip the opt-in hooks (the pre-#381 markup) and both the
    attribute predicates AND the widget's own eligibility check must break."""
    _rows(stub_host, 1)
    body = client.get(f"{UI}/engagements").get_data(as_text=True)
    tag = _table_tag(body)
    plain_tag = _PLAIN_HOOKS.sub("", tag)
    assert plain_tag != tag, "expected the table to carry data-dt hooks to strip"

    attrs = _attrs(plain_tag)
    assert "data-dt" not in attrs
    assert "data-dt-colfilter" not in attrs
    assert "data-dt-page-size" not in attrs

    plain_body = body.replace(tag, plain_tag)
    assert not _widget_eligible(plain_body)   # the hook, not the table shape, is what wires it


def test_the_widget_init_hook_is_wired_and_the_page_is_eligible(client, stub_host):
    """The init path, exercised as far as a non-browser test can (see RESIDUAL in the module docstring):
    the page must satisfy every gate `data-table.js` reads before DataTables can turn the rows into the
    widget, and it must do so through the opt-in hook rather than by accident."""
    names = _rows(stub_host, 4)
    body = client.get(f"{UI}/engagements").get_data(as_text=True)
    attrs = _attrs(_table_tag(body))

    # initAll() finds exactly one table to init on this page, and it is the Report Boards list.
    assert len(_DT_TABLE.findall(body)) == 1
    assert "data-dt" in attrs and attrs.get("id") == "scribble-engagements-table"

    # Every structural gate init() bails on before DataTables touches the table.
    assert _widget_eligible(body)

    # The attributes init() parses to configure paging and the per-column filter row.
    assert int(attrs["data-dt-page-size"]) == 25
    assert "data-dt-colfilter" in attrs
    # The filter row is built from each sortable header's text (`aria-label="Filter by <text>"`), so the
    # sortable headers must carry text rather than an icon or an empty cell.
    thead = body.split("<thead>", 1)[1].split("</thead>", 1)[0]
    sortable_text = [
        re.sub(r"<[^>]+>", "", th).strip()
        for th in re.findall(r"<th\b[^>]*data-sort[^>]*>(.*?)</th>", thead, re.S)
    ]
    assert sortable_text == ["Engagement", "Client", "Report board"]

    # The body init() snapshots holds every row the server sent, so the widget has data to page over.
    tbody = body.split("<tbody>", 1)[1].split("</tbody>", 1)[0]
    assert tbody.count("<tr") == len(names) == 4


def test_the_eligibility_model_rejects_a_table_the_widget_would_skip():
    """Pin the model itself, or it could pass every page by returning True. Each case is a real early
    return in data-table.js: no opt-in hook, no `<thead>` row, nothing in the body to snapshot."""
    no_hook = (
        "<table class=\"table\"><thead><tr><th>a</th></tr></thead>"
        "<tbody><tr><td>1</td></tr></tbody></table>"
    )
    no_head = "<table data-dt><tbody><tr><td>1</td></tr></tbody></table>"
    head_without_row = "<table data-dt><thead></thead><tbody><tr><td>1</td></tr></tbody></table>"
    empty_body = "<table data-dt><thead><tr><th>a</th></tr></thead><tbody></tbody></table>"
    wired = "<table data-dt><thead><tr><th>a</th></tr></thead><tbody><tr><td>1</td></tr></tbody></table>"

    for skipped in (no_hook, no_head, head_without_row, empty_body):
        assert not _widget_eligible(skipped), skipped
    assert _widget_eligible(wired)


def test_every_row_reaches_the_widget_in_the_initial_html(client, stub_host):
    """The widget snapshots `tbody` at init, so the rows have to be server-rendered, all of them: a
    later page must not depend on JS to have data to sort/filter/page."""
    names = _rows(stub_host, 30)

    body = client.get(f"{UI}/engagements").get_data(as_text=True)
    tbody = body.split("<tbody>", 1)[1].split("</tbody>", 1)[0]
    assert tbody.count("<tr") == 30
    assert all(name in tbody for name in names)
    # Derived from the page, not the fixture: the server renders MORE rows than the page size this very
    # table declares, so paging is a client view over the full set and nothing is dropped server-side.
    page_size = int(_attrs(_table_tag(body))["data-dt-page-size"])
    assert tbody.count("<tr") > page_size


# ── the fallback: no host component, still a plain table ───────────────────────────────────────────────


def test_the_list_degrades_to_a_plain_table_without_the_widget(client, stub_host):
    """Standalone scribble has no `data-table.js` route. The page must still be a complete, readable
    table: every row in the markup, no widget asset referenced and no widget chrome pre-rendered, so
    there is nothing blank to degrade from."""
    names = _rows(stub_host, 3)

    assert client.get("/static/data-table.js").status_code == 404  # the component's asset route is absent

    resp = client.get(f"{UI}/engagements")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)

    assert "data-table.js" not in body  # nothing on the page loads the widget standalone
    assert "dataTables" not in body and "jquery" not in body.lower()
    # No `.dt-*` chrome in the markup: the widget has not "already run" and nothing here imitates it, so
    # the readable table is the server's rendering rather than a degraded copy of a JS build.
    assert not re.findall(r"class=\"[^\"]*\bdt-", body)
    tbody = body.split("<tbody>", 1)[1].split("</tbody>", 1)[0]
    assert tbody.count("<tr") == 3 and all(name in tbody for name in names)
    assert "<div class=\"table-scroll\">" in body  # the plain-table wrapper still renders


def test_no_visible_row_renders_the_plain_empty_state(client, stub_host):
    """`data-dt-empty` is the widget's message for a filtered-out list; the page's own zero-row state is a
    different branch of the template and must not depend on the widget existing. With nothing to list the
    template renders a sentence and no table, so there is no empty widget to initialise."""
    stub_host.engagement_summaries_value = []

    resp = client.get(f"{UI}/engagements")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)

    assert "scribble-engagements-table" not in body and not _DT_TABLE.findall(body)
    assert "No engagements you can operate on yet" in body


# ── the drift guard: no host datatable CSS shipped, from ANY of scribble's stylesheets ─────────────────


def test_no_shipped_stylesheet_collides_with_the_host_data_table(client, stub_host):
    """No core CSS redefined (the #380 rule), read across the whole shipped surface rather than the
    template's `<style>` alone.

    Mounted, `static/scribble.css` is linked into the host page on every scribble route, so a rule there
    restyles the host's own `table.dataTable` / `.dt-*` chrome everywhere even though the page template is
    clean. (Reviewer's reproduction: `.data-table tbody td{padding:99px}` plus `.dt-pager{display:block}`
    appended to `static/scribble.css` left a template-only guard green.)"""
    _rows(stub_host, 1)

    offenders = {
        str(path.relative_to(_PKG)): found
        for path, text in _stylesheets().items()
        if (found := _collisions(text))
    }
    assert not offenders, f"shipped stylesheets restyle the host's datatable: {offenders}"

    # And the page's own inline block, which is the half a stylesheet scan cannot see.
    page = client.get(f"{UI}/engagements").get_data(as_text=True)
    inline = "".join(re.findall(r"<style>(.*?)</style>", page, re.S))
    assert inline.strip(), "expected the page to carry its own <style> block"
    assert not _collisions(inline), f"inline page style restyles the host's datatable: {_collisions(inline)}"


def test_the_guard_scans_every_stylesheet_the_page_links(client, stub_host):
    """The scan set has to track the mount, not a hardcoded list: if a route links a stylesheet the guard
    never reads, the collision test above silently stops covering the stylesheet that actually ships."""
    _rows(stub_host, 1)
    page = client.get(f"{UI}/engagements").get_data(as_text=True)

    linked = re.findall(r"<link[^>]+href=\"([^\"]+\.css)\"", page)
    assert linked, "expected the page to link at least one stylesheet"
    scanned = {path.name for path in _stylesheets()}
    for href in linked:
        assert href.rsplit("/", 1)[-1] in scanned, f"{href} is linked but never scanned for host collisions"


@pytest.mark.parametrize(
    "snippet",
    [
        ".data-table tbody td { padding: 99px }",                     # hyphenated class the host carries
        ".dt-pager { display: block }",                               # widget chrome by prefix
        "table.dataTable tbody tr:hover { background: red }",         # the class DataTables applies
        "[data-dt] { display: none }",                                # hits every opted-in host table
        "thead th { color: red } .dt-colfilter-row th { padding: 0 }",  # the filter row
    ],
)
def test_the_collision_guard_bites_on_each_host_selector(snippet):
    """The guard is a regex over text, so it can rot into a no-op silently. Every spelling of the host's
    datatable classes must be caught, including the camelCase one a hyphenated-only guard misses."""
    assert _collisions(snippet), f"the guard let {snippet!r} through"


def test_the_collision_guard_leaves_scribble_page_classes_alone():
    """Guard the guard the other way: scribble's own page classes and CSS comments must pass, so the
    collision test cannot be satisfied by simply banning stylesheets."""
    assert not _collisions(
        "/* not ours to style: the host's .dt-pager and table.dataTable */\n"
        ".scribble-list-head { display: flex; gap: 12px }\n"
        ".scribble-engagements-actions { white-space: nowrap }\n"
        ".table-scroll { overflow-x: auto }\n"
        "table.table tbody td { padding: 6px }\n"
    )


def test_the_template_references_no_host_template_seam():
    """No host template include: the page must render standalone too, so there is no `_data_table.html`
    (or other host partial) to depend on for the table's markup."""
    assert "_data_table.html" not in _TEMPLATE.read_text()
