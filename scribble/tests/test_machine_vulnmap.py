"""Machine API — `ScribbleVulnMap` CRUD + resolution (`/scribble/machine/vuln-map`,
`/scribble/machine/resolve-template`).

Ported from the deleted lotek `tests/test_api_v1_vulnmap.py`. Proves the scan-finding -> library-
template mapping: create/list, the resolver's most-specific-first precedence (`dedupe_prefix` >
`source`+`title_pattern` glob > `source` alone), the stale-mapping guard, and input validation.
Auth/scope RBAC is the host's own concern (see `test_machine_engagements.py`'s module docstring).
"""

from __future__ import annotations

import uuid

import scribble.models as fm

M = "/scribble/machine"


def _two_template_ids(client):
    items = client.get(f"{M}/templates").get_json()["items"]
    assert len(items) >= 2, "expected the seeded scribble library to have >=2 templates"
    return items[0]["id"], items[1]["id"]


def _mk(client, **body):
    r = client.post(f"{M}/vuln-map", json=body)
    assert r.status_code == 201, r.get_json()
    return uuid.UUID(r.get_json()["id"])


def _resolve(client, **body):
    return client.post(f"{M}/resolve-template", json=body).get_json()["template_id"]


# ── create / list / validation ──────────────────────────────────────────────────────────────────


def test_create_validates_inputs(client, stub_host, clean_vuln_map):
    tid, _ = _two_template_ids(client)
    assert client.post(f"{M}/vuln-map", json={"source": "nuclei"}).status_code == 400  # no template_id
    assert client.post(f"{M}/vuln-map", json={"template_id": tid}).status_code == 400  # no match key
    assert (
        client.post(
            f"{M}/vuln-map", json={"source": "x", "template_id": str(uuid.uuid7())}
        ).status_code
        == 404
    )  # template doesn't exist
    assert (
        client.post(f"{M}/vuln-map", json={"source": "x", "template_id": []}).status_code == 400
    )  # non-int template_id -> 400, not 500


def test_create_then_list(client, stub_host, clean_vuln_map):
    tid, _ = _two_template_ids(client)
    r = client.post(
        f"{M}/vuln-map", json={"source": "dalfox", "title_pattern": "*xss*", "template_id": tid}
    )
    assert r.status_code == 201
    listed = client.get(f"{M}/vuln-map").get_json()
    assert listed["count"] == 1 and listed["items"][0]["source"] == "dalfox"


def test_create_rejects_inactive_template(client, stub_host, session_factory):
    tid, _ = _two_template_ids(client)
    with session_factory() as db:
        db.get(fm.VulnerabilityTemplate, tid).active = False
        db.commit()
    resp = client.post(f"{M}/vuln-map", json={"source": "nuclei", "template_id": tid})
    assert resp.status_code == 404


def test_non_string_match_keys_are_400_not_500(client, stub_host):
    tid, _ = _two_template_ids(client)
    assert client.post(f"{M}/vuln-map", json={"source": ["nmap"], "template_id": tid}).status_code == 400
    assert (
        client.post(f"{M}/resolve-template", json={"source": {"a": 1}, "title": "x"}).status_code == 400
    )


# ── resolution precedence: dedupe_prefix > source+title_pattern > source ────────────────────────


def test_resolution_precedence_and_fallback(client, stub_host, clean_vuln_map):
    t_source, t_dedupe = _two_template_ids(client)
    _mk(client, source="nuclei", template_id=t_source)
    _mk(client, dedupe_prefix="nuclei:CVE-2021-", template_id=t_dedupe)

    # dedupe_prefix wins over the source catch-all when the key matches
    assert _resolve(client, source="nuclei", dedupe_key="nuclei:CVE-2021-44228:host:x") == t_dedupe
    # falls back to the source catch-all when no dedupe_prefix matches
    assert _resolve(client, source="nuclei", dedupe_key="nuclei:other:host") == t_source
    # unknown source with no mapping -> null (caller falls back to from_lotek_finding)
    assert _resolve(client, source="whatever", title="anything") is None


def test_resolution_title_glob(client, stub_host, clean_vuln_map):
    tid, _ = _two_template_ids(client)
    _mk(client, source="dalfox", title_pattern="*sql injection*", template_id=tid)
    assert _resolve(client, source="dalfox", title="Reflected SQL Injection in q") == tid
    assert _resolve(client, source="dalfox", title="XSS in name") is None  # pattern doesn't match


def test_stale_mapping_resolves_to_null(client, stub_host, session_factory, clean_vuln_map):
    tid, _ = _two_template_ids(client)
    _mk(client, source="nuclei", template_id=tid)
    assert _resolve(client, source="nuclei") == tid
    with session_factory() as db:  # retire the mapped template
        db.get(fm.VulnerabilityTemplate, tid).active = False
        db.commit()
    # the mapping is now stale -> resolver returns null so the caller falls back cleanly
    assert _resolve(client, source="nuclei") is None
