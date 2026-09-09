"""Pagination + bound on the JSON document list endpoints (`cream/api.py` + `api_pat.py`).

Before, both returned every visible row unbounded (a DoS lever). Now `?limit`/`?offset` page the result
and `has_more` signals truncation — all three list surfaces route through `service.scoped_documents`.
"""

from __future__ import annotations

import cream.service as service

API = "/cream/api/documents"
MACHINE = "/cream/machine/documents"


def test_list_is_bounded_and_signals_has_more(client, make_doc):
    for _ in range(3):
        make_doc()
    body = client.get(API + "?limit=2").get_json()
    assert len(body["documents"]) == 2
    assert (body["limit"], body["offset"], body["has_more"]) == (2, 0, True)

    last = client.get(API + "?limit=2&offset=2").get_json()
    assert len(last["documents"]) == 1
    assert last["has_more"] is False


def test_offset_pages_without_overlap(client, make_doc):
    ids = {make_doc()["id"] for _ in range(4)}
    page1 = [d["id"] for d in client.get(API + "?limit=2&offset=0").get_json()["documents"]]
    page2 = [d["id"] for d in client.get(API + "?limit=2&offset=2").get_json()["documents"]]
    assert not set(page1) & set(page2)              # disjoint pages
    assert set(page1) | set(page2) == ids           # …that together cover every doc


def test_limit_and_offset_are_clamped(client, make_doc):
    make_doc()
    over = client.get(API + f"?limit={service.LIST_MAX_LIMIT + 999}").get_json()
    assert over["limit"] == service.LIST_MAX_LIMIT          # clamped to the max
    assert client.get(API + "?limit=not-a-number").get_json()["limit"] == service.LIST_DEFAULT_LIMIT
    assert client.get(API + "?offset=-5").get_json()["offset"] == 0
    assert client.get(API).get_json()["limit"] == service.LIST_DEFAULT_LIMIT  # default when unspecified


def test_machine_endpoint_is_also_paged(pat_client, engagement_id):
    for i in range(3):
        assert pat_client.post(
            MACHINE, json={"engagement_id": str(engagement_id), "title": f"m{i}"}
        ).status_code == 201
    body = pat_client.get(MACHINE + "?limit=2").get_json()
    assert len(body["documents"]) == 2
    assert (body["limit"], body["has_more"]) == (2, True)
