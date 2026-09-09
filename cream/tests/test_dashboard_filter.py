"""The dashboard's status/kind filters + the row cap (extension UI-maturity sweep).

The filters are enum-validated in the route, which is also what keeps a raw query-arg value off the page
as free text; the cap makes the list bounded (it iterated every visible document before).
"""

from __future__ import annotations

import re
import uuid

import cream.blueprint as bp

UI = "/cream/"


def _row_ids(body: str) -> set[str]:
    # each rendered row carries exactly one view link: /cream/documents/<id>"
    return set(re.findall(r'/cream/documents/([0-9a-f-]+)"', body))


def test_dashboard_filters_by_status(client, make_doc):
    draft = make_doc()
    issued = make_doc()
    assert client.post(f"/cream/api/documents/{issued['id']}/issue").status_code == 200

    both = _row_ids(client.get(UI).get_data(as_text=True))
    assert {draft["id"], issued["id"]} <= both

    only_draft = _row_ids(client.get(UI + "?status=draft").get_data(as_text=True))
    assert draft["id"] in only_draft and issued["id"] not in only_draft

    only_issued = _row_ids(client.get(UI + "?status=issued").get_data(as_text=True))
    assert issued["id"] in only_issued and draft["id"] not in only_issued


def test_dashboard_filters_by_kind(client, make_doc):
    inv = make_doc(kind="invoice")
    quo = make_doc(kind="quote")

    inv_only = _row_ids(client.get(UI + "?kind=invoice").get_data(as_text=True))
    assert inv["id"] in inv_only and quo["id"] not in inv_only

    quo_only = _row_ids(client.get(UI + "?kind=quote").get_data(as_text=True))
    assert quo["id"] in quo_only and inv["id"] not in quo_only


def test_dashboard_ignores_an_unknown_filter(client, make_doc):
    d = make_doc()
    # a bogus enum value is dropped (fail-open, no 500) and everything still shows
    resp = client.get(UI + "?status=not-a-status&kind=whatever")
    assert resp.status_code == 200
    assert d["id"] in _row_ids(resp.get_data(as_text=True))


def test_dashboard_caps_and_announces_truncation(client, make_doc, monkeypatch):
    monkeypatch.setattr(bp, "_DASHBOARD_MAX", 2)
    for _ in range(3):
        make_doc()
    body = client.get(UI).get_data(as_text=True)
    assert "cr-trunc" in body            # the cap is announced, not silent
    assert len(_row_ids(body)) == 2      # …and no more than the cap is rendered


def test_dashboard_read_scopes_to_visible_engagements(client, make_doc, hooks, engagement_id):
    """Mounted, the list is scoped in SQL to the actor's visible engagements — the line this change
    rewrote from a Python `continue` to `WHERE engagement_id IN (vis)`. Empty scope is fail-CLOSED
    (nothing), a foreign scope hides the doc, and only the doc's own engagement shows it."""
    doc = make_doc()

    hooks["visible_engagement_ids"] = frozenset()  # no visible engagements -> nothing, not everything
    assert doc["id"] not in _row_ids(client.get(UI).get_data(as_text=True))

    hooks["visible_engagement_ids"] = frozenset({uuid.uuid7()})  # a foreign engagement only
    assert doc["id"] not in _row_ids(client.get(UI).get_data(as_text=True))

    hooks["visible_engagement_ids"] = frozenset({engagement_id})  # its own engagement
    assert doc["id"] in _row_ids(client.get(UI).get_data(as_text=True))
