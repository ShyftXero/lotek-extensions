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

- [x] Private brand repo `lotek-theme-synoptek` created, authored and pushed (`5ed700b`): palette
      labelled OFFICIAL/PROPOSED/DERIVED, `[print_tokens]`, three latin-subset variable woff2 faces
      (83 KB decoded), the logo lockup, 6 tests, wheel inspected for the entry point + font files.

## Remaining

- [ ] **#103 discovery, wired.** Recover `theme_discovery.py`; the entry point resolves to a
      `() -> str` returning TOML text, so an installed Theme goes through `_parse_theme_toml` — the
      same closed-allowlist grammar as a bundled one. Then make `themes.list_themes`/`get_theme`
      actually consult it, which is the thing that was missing before.
- [ ] **#104 Marks, wired.** Recover `marks.py` **at `bd8d8af`** (three security fixes landed there
      after the version most people would reach for), add a real `[marks]` field to the Theme schema,
      and render the logo. The renderer has zero `logo` references today.
- [ ] **#113 + #105 override Themes.** Operator-uploaded Themes with full CRUD, validated through the
      same parser, admin-gated and audited; plus the per-install default. Override Provenance is
      raster-only for Marks.
- [ ] **theme_files schema** (orchestrator-owned): `[fonts].package` so an installed Theme's fonts
      resolve inside *its own* distribution; weight RANGES for variable fonts; the `[marks]` field.
- [ ] **Font tokens reach the page.** `tokens.FONT_TOKENS` names `--font-body/-display/-mono`, but
      `_CSS` hardcodes `font-family` at ~9 sites with no custom property behind them, so a Theme's
      type stack currently sets variables nothing reads.
- [ ] **#109 acceptance.** Synoptek report off a demo-range engagement, screen **and print**, plus a
      mounted test in lotek.
- [ ] Re-pin scribble in lotek; run the mounted suite.

## Notes / gotchas

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
