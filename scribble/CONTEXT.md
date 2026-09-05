# Scribble

Scribble is the reporting extension: it turns an engagement's findings and evidence into a client
deliverable. This glossary pins the words used when talking about *how a deliverable looks* — an area
where the codebase has historically overloaded one word ("template") across four unrelated concepts.

## Reporting

**Report**:
The client-facing deliverable produced from one Engagement. Rendered to HTML or DOCX.

**Render**:
One act of producing a Report from an Engagement plus a Layout plus a Theme. A Render is a pure
function of those three inputs; it stores nothing.

**Block**:
One top-level section a Report may contain — cover, contents, executive summary, findings, diagrams,
methodology, evidence appendix, activity log. The set of block kinds is closed.
_Avoid_: section, part

**Layout**:
Which Blocks a Report contains and in what order. Carries no appearance — a Layout says "methodology
comes before findings", never what colour anything is.
_Avoid_: template, report template, structure

**Theme**:
How a Report looks: its palette, typefaces, and marks. Carries no structure — a Theme cannot add,
remove, or reorder a Block.
_Avoid_: skin, style, palette (a palette is only the colour part of a Theme)

Layout and Theme are orthogonal on purpose: any Layout renders under any Theme.

**Reference (value object)**:
A citation on an EngagementFinding — `{label, url, source, suppressed}` (#624). NOT a child entity: a
reference has no lifecycle or evidence of its own, so it lives as a JSON value object in the finding's
`references` column, not its own table. `label` is client-facing ("CWE-89", "Vendor advisory KB123"); a
bare-URL reference uses the URL as its own label. Rendered as an omit-when-empty labeled-link block
(non-suppressed only).
_Avoid_: link, citation, source (the last is the reference's ORIGIN axis, below)

**Reference source**:
Where a reference came from — `template` (the matched library template's refs), `scan` (the scan
finding's `DTO.references`), or `author` (a human added it in the editor). Promote UNIONS the template +
scan refs, deduped by normalized URL; on collision the operator/`author` wins. Stored, but hidden in the
report by default.

**Reference suppress**:
A per-reference `suppressed` flag an operator sets to drop one noisy reference from the report while
keeping the others. Suppressing is per-reference on the finding, not deleting — the reference stays on
the row and out of the deliverable.

**Finding metadata (cve_ids / cwe_ids / owasp_categories)**:
Structured vulnerability classification on an EngagementFinding (#625), typed JSON id lists. Promote
seeds `cve_ids` from `DTO.cve` and `cwe_ids` from `DTO.facts["cwe"]`; `owasp_categories` is DERIVED from
`cwe_ids` via a static offline CWE→OWASP-Top-10-2021 map. Distinct from the free-text `category` (a human
label). Rendered as finding-header chips + compact CWE/CVE index columns, omit-when-empty.

**Threat-intel snapshot (as_of / kev / epss)**:
A DATED point-in-time KEV/EPSS lookup on a finding's CVEs — `{as_of, source, cves:{kev, epss, …}}` in the
`threat_intel` column (#625). NOT bare `kev`/`epss` columns: KEV/EPSS change over time and their source
(the exploiteer extension) is optional, so the `as_of` is mandatory and the report always says "KEV as of
`<date>`" rather than asserting a stale fact as current. Enrichment-managed (computed by the enrichment
driver from exploiteer's verdict feed, degrading to none when absent), never hand-typed.

## Themes

**Token**:
One named value inside a Theme — a colour, a type stack, a radius. The set of tokens a Theme may set
is closed; a value outside it is not a Theme.

**Mark**:
A Theme's graphical identity assets — logo, shapes — as distinct from its Tokens.

**Brand**:
The identity of the firm a Report is issued *from*. A Brand is expressed as a Theme; the two are not
synonyms, because a Theme need not represent anyone's Brand (light and dark do not).
_Avoid_: branding, white-label

**Provenance**:
Where a Theme came from, which fixes how much it is trusted: **bundled** (ships inside Scribble),
**installed** (a separate package Scribble discovers), or **override** (data an operator supplied at
runtime). Bundled and installed Themes are code; an override is data.

**Snapshot**:
The resolved Tokens and Marks frozen onto an Engagement when its Report is delivered, so that
re-theming the install later cannot restyle a Report already in a client's hands.
_Avoid_: cache, freeze, pin

## Adjacent terms that are NOT the above

**Document Template**:
A `.docx` file containing Jinja markup, filled in at render time. A Document Template is not a
Layout and not a Theme — it is a file the DOCX renderer consumes.
_Avoid_: template, docx template (unqualified)

**Vulnerability Template**:
Reusable finding boilerplate in the Vuln Library — title, description, remediation — that an author
copies into an Engagement. Unrelated to Reports entirely.
_Avoid_: template (unqualified)

**Report Disposition**:
What one Finding's `status` means for the deliverable: `live` (renders **and** drives the overall risk
rating), `remediated` / `accepted` (render, but leave the risk ladder), `excluded` (a false positive —
absent from the deliverable entirely). Derived by the single predicate
`scribble.enums.report_disposition`, which the report context, both renderers and the board all read
rather than re-deriving. A property of a **finding**, at report time.
_Avoid_: disposition (unqualified), status (when you mean its report consequence)

**Field Disposition**:
Where a `FindingDTO` field lives on an `EngagementFinding` — a typed `column`, the verbatim
`source_facts` snapshot, or a reasoned `drop` — plus its `origin` (promote / author / enrichment) and
`operator` (locked / editable) axes. Declared per DTO field in `scribble/dispositions.py` and held in
place by a drift guard. A property of a **schema field**, at promote time.
_Avoid_: disposition (unqualified)

> These two arrived a day apart (lotek#618 and lotek#617) and share nothing but the word. One is a
> finding's fate in a document; the other is a column's provenance. If you write "disposition"
> unqualified in this package, a reader has to guess which — so don't.
