# Plan: feat/scribble-report-themes

- **Branch:** `feat/scribble-report-themes`  (worktree: `.claude/worktrees/scribble-report-themes`, off `main`)
- **PR:** #119 (opened 2026-08-26)
- **Status:** 🟡 in review — scope CUT to the Layout/Theme split + bundled loader (see 'Scope cut' below)

## Purpose

Scribble can only be re-skinned by editing Python. Report appearance lives as a `theme: str` field on the
frozen `ReportTemplate` dataclass (`scribble/reporting/templates.py`), selectable only via `?template=`,
never persisted, with no hook for a logo or a typeface. This branch makes appearance a first-class,
configurable thing: **Layout** (which blocks, in what order) split from **Theme** (palette, type, marks),
with light and dark bundled and a firm-brand Theme installable from outside the wheel.

Vocabulary for all of this is pinned in `scribble/CONTEXT.md` — read it before naming anything.

## Done

- [x] `scribble/CONTEXT.md` — glossary: Layout / Theme / Token / Mark / Brand / Provenance / Snapshot,
      and the four things "template" used to mean.
- [x] **#100 — split `Theme` from `Layout`** (`5c32f20`). `reporting/layouts.py` (structure only) +
      `reporting/themes.py` (appearance only) + `reporting/selection.py` (resolves the pair, and
      translates the pre-split `?template=` so bookmarked deliverable URLs survive). The dataclass
      `ReportTemplate` is now `ReportLayout` and `reporting/templates.py` is gone, which frees the word
      "template" for the three things in this extension that already meant it. `light` became
      selectable for the first time. The full Layout × Theme matrix is tested — it was previously
      unrepresentable, since a pairing existed only if someone had added a registry row for it.
      Reporting suites green (44 passed); ruff + pyrefly clean.

## Remaining

Tier A (the shipping target):
- [ ] Closed Token allowlist + per-token validators; `:root{}` override block emitted after the base sheet.
      Reject-and-fall-back wholesale, never partially apply.
- [ ] Retire the 4 literal colours that bypass the token layer (`#fff` ×3, and the print block's `a`).
- [ ] Font Tokens: `font-family` is hardcoded in 9 places; route through tokens.
- [x] Bundled Themes as `scribble/report_themes/{light,dark}.toml` + sibling `.woff2`, read via
      `importlib.resources`. **CORRECTION:** an earlier draft of this plan said these "must be hatch
      `force-include`d or they are silently absent from the wheel". That is WRONG and
      `theme_files.py`'s module docstring is the accurate one: `pyproject.toml` already declares
      `packages = ["scribble"]`, so this directory ships inside the wheel with no `force-include` —
      verified by building an actual wheel and listing it. The `force-include` trap is real for
      `lotek-extension.toml`, which sits at the PROJECT ROOT, outside the package dir; these files do
      not.
- [ ] Installed-Theme discovery over entry-point group `scribble.report_themes`.
- [ ] Mark support: a logo hook (the renderer has **zero** `logo` references today).
- [ ] Per-install default Theme: new `scribble_settings` singleton on cream's `slot`-unique pattern.
      Admin-gated (`current_actor_is_admin()`, cream's exact gate) + audited via `api_pat._audit`.
- [ ] Snapshot write: `scribble_engagements.report_theme` (nullable = inherit) and
      `report_theme_snapshot` (JSON, frozen at delivery). **Tier A even though its UI is Tier B** —
      retrofitting it means re-deriving branding history that no longer exists.
- [ ] Acceptance: a firm-branded Report rendered off a demo-range Engagement with realistic finding
      volume, checked **on screen and in print**, plus a mounted test in lotek's `tests/test_scribble_*`.

Tier B (after):

- [ ] Override Themes: `scribble_report_themes` rows + the admin edit UI.
- [ ] Per-Engagement Theme override picker.
- [ ] Themed DOCX: pass Theme tokens into `report_templates/build_default_docx.build()`.

Tier C (fog):

- [ ] Operator-uploaded Document Templates (docxtpl/Jinja over `.docx`) — this is what the long-dead
      `scribble_report_templates` table was designed for; leave it untouched until then.

## Notes / gotchas

Decisions settled by grilling before any code (all five rounds):

- Destination is **change in place**, scoped to Scribble's report deliverables only. Scribble's own app
  chrome (`base.html` hardcodes `data-theme="dark"`) and any platform-wide theme layer shared with
  lotek core / cream's independent `Brand` singleton are **out of scope** — that's a second effort.
- **No tactical stopgap.** A hardcoded brand palette in `_CSS` was considered and ruled out.
- Theme trust follows Provenance: **bundled and installed Themes may carry SVG Marks** (installing a
  package is already arbitrary code execution, so its SVG is not the weak link); **override Themes are
  raster-only**. cream refuses SVG outright for exactly this reason (`cream/cream/api.py:122`, and it
  was a real fixed bug there) — this is that rule refined by trust level, not a relaxation of it.
- **Closed Token allowlist, not arbitrary CSS.** By Tier B the payload is admin-typed text injected into
  a document that also embeds client evidence; arbitrary CSS there is an exfiltration primitive
  (`background:url(…)`) in a deliverable stamped CONFIDENTIAL.
- **Fonts embed as base64 `.woff2`** (latin subset), gated per-Theme. A Google Fonts `<link>` is
  disqualified on confidentiality, not just offline-ness: it makes every open of a client report an
  outbound request carrying the referer.
- DOCX cannot embed fonts (python-docx), so a themed `.docx` *names* its typeface and substitutes where
  it is absent. Accepted: DOCX is the editable working copy, HTML/PDF is what the client receives.
- Do **not** reuse `TemplateVariable` (`models.py`, unique key→value) for settings — it means report
  `{{variables}}`, and overloading it re-collapses the vocabulary this branch is untangling.
- New tables use `ScribbleUuid` + `uuid7` (the breaking PK migration landed in `360ab13`); host
  references use `SoftHostId`, never Integer/String.
- The `@media print` block carries a **fourth** palette with selectors deliberately covering no-stamp /
  light / dark (re-engineered in `1c00281`). A screen-only check will pass while the printed deliverable
  comes out in generic slate — hence print being part of acceptance.
- `BLOCK_KEYS` is 8 blocks now, two of them (`cover`, `toc`) print-only. A Theme must not disturb them.

Pre-existing failure on `main`, found while landing #100 and deliberately NOT fixed here:
`test_machine_artifacts.py::test_upload_emits_audit_row` and `::test_update_emits_audit_with_transition`
compare an audit `subject_id` (a `uuid.UUID`) against a JSON id string — UUIDv7-migration drift.
Confirmed unrelated to reporting: that file imports nothing from `reporting` and contains zero
layout/theme references. Worth its own issue.

Open, pending the human: whether the firm-brand Theme package and this map's tickets may live in a
**public** repo, given the source brand material is marked draft-and-unadopted.


## Scope cut (2026-08-26, before merge)

Two independent reviews of the full branch diff found ~840 lines of production code with **no caller**,
and demonstrated by execution that this directory's documented "add a bundled Theme" procedure silently
does nothing. Rather than merge a feature whose headline capabilities are each unreachable, the branch
was cut down to what is actually wired:

**Kept (wired, and what the PR now claims):** `layouts.py`, `themes.py`, `selection.py`, `theme_css.py`,
`theme_files.py`, `tokens.py`, the bundled `light`/`dark` TOMLs, and their tests — the Layout/Theme
split plus a real bundled-Theme loader.

**Cut, each to return with the code that calls it:**

| Cut | Returns with |
|---|---|
| `reporting/theme_discovery.py` + tests (entry-point Theme discovery) | the ticket that makes `get_theme` consult it |
| `reporting/marks.py` + tests (logo vetting, Provenance-gated SVG) | ext#104 — the Theme schema field that carries a logo |
| `ScribbleSettings`, `Engagement.report_theme`, `report_theme_snapshot`, migration `b8d947be11b3` | ext#105/#106 — the UI that reads and writes them |

Two things to carry forward rather than rediscover:

1. **`marks.py` was cut AFTER three security fixes landed on it** (commit `bd8d8af`): a CSS-escape
   bypass in the attribute filter (`\75 rl(` reconstitutes as a real `url()` token), magic-byte
   verification on raster payloads (an SVG relabelled `image/png` was accepted as raster, routing around
   the Provenance rule entirely), and an allow-list Provenance gate (the deny-list defaulted an unknown
   provenance to PERMITTING SVG, behind an `assert` stripped under `python -O`). **Recover that
   revision, not the one before it.**
2. **`ScribbleSettings` may not be needed at all.** The `feat/extension-settings-ui` work concluded
   that a per-install default belongs in the host's existing `settings` table via one `[[settings]]`
   manifest entry — no new table, no migration. Settle that before re-adding this one.

Also fixed here: the Layout switcher wrote only its own axis, so changing the Layout on a bookmarked
`?template=dark` URL silently discarded the Theme (`?layout=…` with no theme resolves to `auto`) —
defeating the guarantee `selection.py` exists to provide. Its guard test asserted the presence of a
literal source string and passed either way; it now pins both axes and the absence of the broken form.
