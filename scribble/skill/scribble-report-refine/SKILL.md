---
name: scribble-report-refine
description: Use this skill to refine the scribble report's executive summary and narrative prose for a client-facing pentest deliverable — tightening the risk-led framing and finding write-ups for a non-technical reader without touching the underlying data the report is built from.
---

# Scribble Report Refine

This skill helps polish the READING of a Scribble engagement report — the executive summary framing,
the narrative flow between findings, transitions, and plain-language explanations of impact — for the
kind of client-facing pentest deliverable this repo's Ascension-reporting house style expects. It does
**not** run the assessment, choose findings, or decide risk.

## When to use this

The user has an existing Scribble engagement (findings already promoted / authored, groups already
assigned) and wants the report's prose tightened: a stronger executive summary, a clearer narrative arc
across the finding groups, plainer language for a non-technical stakeholder, or better transitions
between an internal / external / web-app / device-mobile section and the next.

## Input: the context sidecar, not the database

Never query `scribble_findings` or any other Scribble table directly, and never open the Scribble
SQLite/Postgres database with a writable connection. The one sanctioned way to read an engagement's
current state is `scripts/context_sidecar.py` in this skill directory:

```
python scripts/context_sidecar.py --db /path/to/scribble.db --engagement-id 42 --out /tmp/report.context.json
```

(or `--engagement-name "Acme Q3 Assessment"` in place of `--engagement-id`). The script opens the
database in SQLite's own **read-only URI mode** (`mode=ro`), enforced at the connection level — not by
convention — and writes a plain-JSON sidecar file shaped like the report's `ReportContext`: top-level
`engagement_id` / `engagement_name`, a `groups` list (each with `id`, `name`, `type_slug`, and its
`findings`), a `rollup` (`counts` / `total` / `overall`), and the resolved template `variables`. Each
finding carries `severity`, `cvss` fields (`cvss_score` / `cvss_vector`), rendered `blocks_html`, and its
`artifacts`. Read the sidecar JSON as the sole source of engagement facts; do not infer values not
present in it.

## The guardrail: never change the data

This skill drafts and suggests PROSE. It must **never** alter a finding's severity, cvss score/vector,
target, or evidence — those are the analyst's determinations, verified and owned outside this skill, and
rewriting them here would silently misrepresent the assessment to the client. Treat every `severity`,
`cvss_score`/`cvss_vector`, and `evidence`/artifact value in the sidecar as read-only ground truth:
quote it, explain it, build a narrative around it — never invent a different number, never soften or
escalate a rating, never add or drop an artifact. This is the hard guardrail for this skill: it produces
words, never a data change. If a finding looks miscategorized, say so back to the analyst instead of
"fixing" it yourself.

Concretely, this skill must never:

- edit `scribble_findings.severity`, `.cvss_score`, `.cvss_vector`, or any evidence/artifact record;
- call any Scribble write endpoint, form, or ORM session that isn't the read-only sidecar above;
- fabricate a finding, host, or evidence item not present in the sidecar JSON.

Note also that read-only is the *only* thing the sidecar enforces: it performs **no per-engagement
authorization** of its own, and never checks tenancy/membership. Point `--db`/`--engagement-id` only at
a database file the invoking operator is already fully authorized to read end-to-end -- the sidecar will
happily open and return data from any engagement in whatever file it's given.

## Output

Produce refined prose (executive summary paragraph(s), narrative transitions, plain-language impact
framing) as text/markdown for the user to paste into the engagement's report editor themselves, or as a
diff-able suggestion — never as a direct database write. See the `Ascension-reporting` skill for the
underlying house style (risk-led executive summary, finding narrative shape, evidence hygiene) this
skill's suggestions should match.

## Reference

`references/methodology.md` covers how to phrase risk framing per assessment type (internal, external,
web-app, device-mobile) — read it before drafting an executive summary that spans more than one type.
