"""Drift guard (Task V): every ``{{TOKEN}}`` referenced by a shipped lotek vuln-DB template
(``fraction/seed/lotek_vulnerabilities.json``) must be a variable the pipeline can actually populate --
either a structural ``BUILTIN_KEYS`` name or a DB-declared ``TemplateVariable`` (``fraction/seed/
report_variables.json``, CONTRACT-FACTS.md §4.3). A future template edit that types a new ``{{TOKEN}}``
without also declaring/populating it renders VERBATIM in the client deliverable -- this is the exact bug
PR #182 fixed (issue-182: literal ``{{DOMAIN}}``/``{{AFFECTED}}`` reaching the report). This test uses
the SAME ``lint_doc``/``known_variable_keys`` machinery the live preview/lint feature uses, so it can
never drift from what the real pipeline considers "known" -- no hardcoded token list to keep in sync by
hand.
"""

from __future__ import annotations

import json
from pathlib import Path

from fraction.models import VulnerabilityTemplate
from fraction.templating import known_variable_keys, lint_doc

_LOTEK_SEED = Path(__file__).parent.parent / "fraction" / "seed" / "lotek_vulnerabilities.json"


def _lotek_template_names() -> set[str]:
    records = json.loads(_LOTEK_SEED.read_text())
    return {r["Name"] for r in records if r.get("Name")}


def test_lotek_seed_file_has_19_records():
    # Pins the count independently of test_seed_content.py's DB-level 63 = 44 + 19 assertion, so a
    # record silently added/removed from the JSON is caught here too, with the file as the direct cause.
    assert len(_lotek_template_names()) == 19


def test_every_lotek_template_token_is_known_to_the_pipeline(session_factory):
    """No ``{{TOKEN}}`` in any shipped lotek template resolves to nothing. A regression here means a
    template was edited to reference a variable that is neither a builtin nor declared in
    ``report_variables.json`` -- exactly the literal-token-leak bug #182 fixed."""
    names = _lotek_template_names()
    with session_factory() as db:
        known = known_variable_keys(db)
        templates = db.query(VulnerabilityTemplate).filter(VulnerabilityTemplate.name.in_(names)).all()
        assert len(templates) == len(names), "a lotek seed entry failed to import"

        unknown_by_template: dict[str, list[str]] = {}
        for tmpl in templates:
            unknown: set[str] = set()
            for _block, doc in (tmpl.content_json or {}).items():
                unknown.update(lint_doc(doc, known_keys=known))
            if unknown:
                unknown_by_template[tmpl.name] = sorted(unknown)

    assert unknown_by_template == {}, (
        f"lotek vuln-DB templates reference unfed token(s): {unknown_by_template!r} -- "
        "either declare the fact -> variable mapping in fraction/seed/report_variables.json, or fix the "
        "template's prose to stop referencing it"
    )


def test_only_the_expected_token_vocabulary_is_used():
    """A tighter pin than the lint-based test above: the specific set of tokens the 19 shipped templates
    actually use today. Guards against silent scope creep (e.g. someone reintroducing a raw ACCOUNTS/
    OBJECTS/AFFECTED_COUNT/MAX_SEVERITY reference in body prose, which -- per the notes for this task --
    is deliberately NOT done in the seed content because those variables have no non-empty fallback and
    would dangle when the underlying fact is absent, unlike AFFECTED's cascade). Update this set
    deliberately if a future edit intentionally adds a new token AND has verified it degrades safely.
    """
    import re

    records = json.loads(_LOTEK_SEED.read_text())
    tokens: set[str] = set()
    for rec in records:
        blob = (rec.get("Description") or "") + (rec.get("Recommendation") or "")
        tokens.update(re.findall(r"\{\{(\w+)\}\}", blob))

    assert tokens == {"AFFECTED", "DOMAIN", "TARGET_HOST", "TARGET_URL"}
