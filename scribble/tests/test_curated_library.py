"""The curated library + assessment-bucket checklists (``curated_vulnerabilities.json`` +
``seed/checklists/{bucket}.json``) and the bulk machine-import route.

Guards:
  * the merged library seeds (richer than FACTION: tags + references + CVSS carried through);
  * NO seeded template carries an unresolved ``{{token}}`` that isn't a known builtin — the whole-library
    jinja-safety property, since scribble jinja-renders content and one bad ``{{ }}`` blanks the block;
  * the seven assessment buckets seed as coverage checklists with items;
  * ``POST /machine/templates/bulk`` creates many, is idempotent by name, and marks rows machine-authored.
"""
from __future__ import annotations

import re

from scribble.enums import ChecklistKind, Severity
from scribble.models import ChecklistTemplate, VulnerabilityTemplate

M = "/scribble/machine"

# Valid double-brace tokens after seeding: the COMPANY_NAME builtin + the report-variable keys.
KNOWN_TOKENS = {
    "COMPANY_NAME", "AFFECTED", "DOMAIN", "TARGET_URL", "TARGET_HOST",
    "MAX_SEVERITY", "AFFECTED_COUNT", "ACCOUNTS", "OBJECTS",
}
_DB_RE = re.compile(r"\{\{\s*([^{}]*?)\s*\}\}")

BUCKETS = {
    "external-network": "external",
    "internal-network-unauth": "internal",
    "internal-ceded-access": "internal",
    "webapp-unauth": "web-app",
    "webapp-auth": "web-app",
    "cloud-assessment": "cloud",
    "red-team-vapt": "red-team",
}


def test_merged_library_seeds_with_tags_and_references(session_factory):
    with session_factory() as db:
        # a representative merged entry exists and carries applicability tags
        tls = db.query(VulnerabilityTemplate).filter_by(name="Insecure SSL/TLS Configuration").one()
        tag_names = {t.name for t in tls.tags}
        assert {"scope:external", "scope:internal"} & tag_names, tag_names
        # a distinct root cause was NOT merged into the one above (true-variants-only policy)
        assert db.query(VulnerabilityTemplate).filter_by(
            name="Insecure SSL/TLS Certificate Configuration").count() == 1
        # the merged library is sizeable (sanity floor, not an exact pin — that lives in test_seed_content)
        assert db.query(VulnerabilityTemplate).count() >= 300


def test_no_seeded_template_has_an_unresolved_jinja_token(session_factory):
    """Every ``{{ }}`` in rendered content must be a known builtin; anything else blanks the block at
    report render. Covers the whole seeded library (FACTION + lotek + curated)."""
    offenders: list[tuple[str, str]] = []
    with session_factory() as db:
        for tmpl in db.query(VulnerabilityTemplate).all():
            blob = " ".join(str(v) for v in (tmpl.content_html or {}).values())
            for raw in _DB_RE.findall(blob):
                key = raw.split("|")[0].split()[0].strip() if raw.strip() else ""
                if key.upper() not in KNOWN_TOKENS:
                    offenders.append((tmpl.name, raw[:40]))
    assert offenders == [], f"unresolved jinja tokens in seeded templates: {offenders[:10]}"


def test_assessment_bucket_checklists_seed(session_factory):
    with session_factory() as db:
        for slug, category in BUCKETS.items():
            tmpl = db.query(ChecklistTemplate).filter_by(slug=slug).one()
            assert tmpl.builtin is True
            assert tmpl.kind == ChecklistKind.coverage
            assert tmpl.category == category
            assert len(tmpl.items) > 0, f"{slug} has no items"


def test_bulk_create_templates_is_idempotent_and_machine_authored(client, stub_host, session_factory):
    payload = {"templates": [
        {"name": "Bulk Alpha", "severity": "high", "tags": ["scope:external"],
         "Description": "# Description\n<p>Alpha body for {{COMPANY_NAME}}.</p>", "Recommendation": "<p>Fix.</p>"},
        {"name": "Bulk Beta", "severity": "low", "references": ["https://example.test/beta"]},
    ]}
    r = client.post(f"{M}/templates/bulk", json=payload)
    assert r.status_code == 201, r.get_json()
    body = r.get_json()
    assert body["created"] == 2 and body["skipped"] == 0 and len(body["ids"]) == 2

    # idempotent by name: a replayed body creates nothing new
    r2 = client.post(f"{M}/templates/bulk", json=payload)
    assert r2.status_code == 201, r2.get_json()
    assert r2.get_json()["created"] == 0 and r2.get_json()["skipped"] == 2

    with session_factory() as db:
        alpha = db.query(VulnerabilityTemplate).filter_by(name="Bulk Alpha").one()
        assert alpha.machine_authored is True
        assert alpha.default_severity == Severity.high
        assert {t.name for t in alpha.tags} == {"scope:external"}


def test_bulk_create_templates_rejects_bad_input(client, stub_host):
    assert client.post(f"{M}/templates/bulk", json={}).status_code == 400
    assert client.post(f"{M}/templates/bulk", json={"templates": "nope"}).status_code == 400
    assert client.post(f"{M}/templates/bulk", json={"templates": [{"category": "x"}]}).status_code == 400
    too_many = {"templates": [{"name": f"T{i}"} for i in range(1001)]}
    assert client.post(f"{M}/templates/bulk", json=too_many).status_code == 413
