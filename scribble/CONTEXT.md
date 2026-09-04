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

## Retest

**Retest**:
One verify-the-fix round recorded against a Finding after remediation. Belongs to the Finding (not
directly to the Engagement — it reaches the Engagement through its Finding), carries who tested it and
when, and dies with the Finding.
_Avoid_: recheck, revalidation, verification (unqualified)

**Retest Outcome**:
The verdict of a Retest — *remediated*, *partially remediated*, *not remediated*, *accepted risk*, or
*not tested*. It is the one thing that moves the Finding's status, and it does so in exactly one place
(`findings_service.record_retest`): a verified fix closes the Finding, an unresolved one reopens it for
another round, an accepted risk records the client's decision, and an untested round leaves the status
untouched.
_Avoid_: result, disposition (a "disposition" here is the `FindingDTO`→`EngagementFinding` mapping)

## Adjacent terms that are NOT the above

**Document Template**:
A `.docx` file containing Jinja markup, filled in at render time. A Document Template is not a
Layout and not a Theme — it is a file the DOCX renderer consumes.
_Avoid_: template, docx template (unqualified)

**Vulnerability Template**:
Reusable finding boilerplate in the Vuln Library — title, description, remediation — that an author
copies into an Engagement. Unrelated to Reports entirely.
_Avoid_: template (unqualified)
