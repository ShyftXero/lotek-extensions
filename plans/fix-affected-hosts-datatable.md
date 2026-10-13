# fix/affected-hosts-datatable

- **Status:** releasing (fast path per Eli)

## Purpose
The finding-detail "Affected hosts" table was a plain full-height table — a finding with dozens of hosts
ran the page tall. Opt it into the shared DataTable widget (core static/data-table.js, loaded by the
host base) so it paginates/sorts like the board rollups. Server-rendered + static, so it meets the
widget's "no rows appended after init" contract.

## Done
- finding.html: affected-hosts <table> gets `table` class + `data-dt data-dt-page-size="25"` + id + empty.
- test_board.py::test_affected_hosts_table_uses_the_shared_datatable. 66 related green.
