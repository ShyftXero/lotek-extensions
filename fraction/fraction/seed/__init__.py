"""Seed data: default assessment types, built-in variables, the vulnerability-template library
imported/converted from FACTION's ``faction_vulnerabilities.json``, the lotek scan-finding ->
library-template mapping (``FractionVulnMap``, seeded from ``lotek_vuln_map.json``), and the
declarative fact -> report-variable mapping (``TemplateVariable.from_facts``/``.target_column``,
seeded from ``report_variables.json``)."""

from fraction.seed.loader import (  # noqa: F401
    import_checklist_templates,
    import_vuln_templates,
    seed_assessment_types,
    seed_builtin_variables,
    seed_defaults,
    seed_report_variables,
    seed_vuln_map,
)
