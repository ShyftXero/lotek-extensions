# Plan: feat/scribble-theme-operational

- **Branch:** `feat/scribble-theme-operational`  (worktree: `.claude/worktrees/scribble-report-themes`, off `origin/main` @ 926ca39)
- **PR:** not opened yet
- **Status:** 🟡 in progress

## Purpose

Make report Theming actually usable. The engine landed in #119 (Layout/Theme split + a bundled Theme
loader), and commit `75159ed` then correctly **cut** everything that had no caller — installed-Theme
discovery, Marks, and the settings/snapshot schema — recording "each to return with the code that
calls it". This branch is that return: it brings them back **with their callers**, adds the
operator-facing CRUD the engine was designed for, and renders a real firm brand end to end.

The firm brand itself lives outside this repo, in the private **`ShyftXero/lotek-theme-synoptek`**,
installed through the `scribble.report_themes` entry point. See "Why the brand is not here" below.

## Done

- [x] Private brand repo `lotek-theme-synoptek` created, authored and pushed (`5ed700b`, `109c93a`):
      palette labelled OFFICIAL/PROPOSED/DERIVED, `[print_tokens]`, three latin-subset variable woff2
      faces (83 KB decoded), the logo lockup, 6 tests, wheel inspected for the entry point + fonts.
- [x] **Type stacks reach the page** (`24f80f0`). `FONT_TOKENS` existed but `_CSS` hardcoded
      `font-family` at nine sites with nothing reading the variables. Five near-duplicate monospace
      stacks collapsed onto one `--font-mono`.
- [x] **Schema an installed Theme can satisfy** (`24f80f0`): `[fonts].package`, variable-font weight
      RANGES, real `[marks].logo_svg`.
- [x] **`[identity].stamp`** (`7d414a5`) — a Theme declares which palette it is tuned for.
- [x] **#103 discovery + #104 Marks recovered, #113/#105 override CRUD** (`f2f14fe`). Marks recovered
      at `bd8d8af` — the verifier diffed it and got zero output, so all three security fixes survived.
- [x] **#103/#104 WIRED** (`c92534a`): `reporting/theme_registry.py` resolves a name across all three
      Provenances; `theme_css` became a pure composer; the Mark renders in the masthead; the switcher
      lists every Provenance; `themes_api` registered with a sidebar entry.
- [x] Theme routes classified for the tenancy gate (`dba4d4a`).
- [x] Security review fixes (`2ab22b3`): `[fonts].package` gated by Provenance, the per-install
      default given a reader, mixed-case names folded, name/label length-bounded.
- [x] **Proven end to end against a real install**: entry point discovered, listed in the switcher,
      resolves as `installed`, stamps `data-theme="light"`, themes screen AND paper, embeds 3 woff2
      faces as base64 with zero remote references, renders its SVG Mark — and the same Mark under
      `override` provenance is refused.

## Remaining

- [ ] **#106 delivery Snapshot.** Still unbuilt, and its crux question is still unanswered: WHAT EVENT
      counts as delivery (first export? every export? an explicit action?). A snapshot taken at the
      wrong moment is worse than none. This is the one piece that does not retrofit cleanly.
- [ ] **#109 acceptance, the mounted half.** The engine is proven against a real install (above), but
      CLAUDE.md is explicit that a stub host proves logic and never the mount — the override CRUD
      authorizes, so it needs a test in lotek's `tests/test_scribble_*`.
- [ ] Re-pin scribble in lotek and run the mounted suite.
- [ ] #107 brand sign-off on the two PROPOSED severity values.


## Notes / gotchas

**A correction to `dba4d4a`'s commit message.** It states the full suite was "green apart from the two
pre-existing `test_machine_artifacts.py` audit-UUID failures". That is wrong. Those two were failing on
the *earlier* branch in August; they were fixed on `main` in the 84 commits between, and the run that
message cited actually showed three different failures (two `resolve_selection` assertions and the
tenancy-gate classification), all fixed in that same commit. The claim was carried from stale memory
rather than verified. **The full suite on this branch is green: 0 failures, 0 errors.**


**Why the brand is not in this repo.** `ShyftXero/lotek-extensions` is public and lotek installs all
six bundled extensions from it as pinned `https://` git deps **with no credentials**, including inside
`deploy/Dockerfile.dashboard`'s `uv sync --frozen --no-dev --extra extensions`. Making this monorepo
private would have broken the production image build, every uncredentialed clone, and the serve loop.
Hence the third repo: engine public, brand private, and **lotek does not pin the brand repo** — only
an instance that wants Synoptek branding installs it, so the public prod image is unaffected.

**The disclosure line, concretely.** Palette values, typeface names and the logo are in the private
repo. The draft brand-book addendum and its proposed amendment are **not committed anywhere** — they
stay in `~/Downloads/synoptek_security/`. Two severity values are marked PROPOSED and pending #107;
the palette supplies four usable signal colours for five severity tiers.

**Recover, don't rebuild.** `git show 'bd8d8af:…/marks.py'` and
`git show '75159ed^:…/theme_discovery.py'`. Rewriting from scratch silently drops the security fixes
and the case-folding collision fix.

**Specificity is load-bearing, and `tokens.py`'s docstring is now stale about it.** It says
`render_token_block` emits a plain `:root` (0-1-0) that always loses to the dark and print rules —
true when written, but the emitter now takes a selector and `theme_css` passes `:root:root` (0-2-0)
inside `@media screen`. Do not "fix" the code to match that paragraph; fix the paragraph.

**Paper is opt-in and must stay that way.** `test_report_print_media.py::test_a_dark_template_still_prints_on_paper_colours`
is the guard. An earlier cut of `theme_css` carried "brand identity" tokens to paper automatically and
that test caught the dark theme's screen orange printing over the paper ramp.

**A stub host proves logic, never the mount** (CLAUDE.md). Anything touching authz — and the override
CRUD does — needs a mounted test in lotek's `tests/test_scribble_*`.

**GPG signing** is on globally and pinentry cannot prompt in this environment; the brand repo has
`commit.gpgsign=false` set locally.
