# Plan: fix/scribble-checklist-select-theming

- **Branch:** `fix/scribble-checklist-select-theming`  (worktree: `.claude/worktrees/ux-checklist-theming`, off `main`)
- **PR:** not opened yet
- **Status:** 🟢 ready to merge — **adversarial review round 2 applied** (round 1: REPAIR, 0 blocking,
  2 CONCERNs; round 2: REPAIR, 0 blocking, 1 CONCERN. All three accepted and fixed, none refuted — see
  "Review round 1" / "Review round 2" below. Both rounds were run by an *independent agent reviewer*,
  not by the author; the verdicts quoted here are theirs.)
- **Closes:** ShyftXero/lotek-extensions#44

## Purpose
The engagement board's coverage-checklist panel renders its per-item status controls as **unstyled
native `<select>` boxes** (measured `rgb(239,239,239)`, OS chrome) against the dark themed panel — one
per checklist item — plus an empty dashed rectangle above the list. Client-reported from a real
TeamsPlus deliverable: *"coverage checklist looks broken while editing — styles are off in the editor
view."*

## Done
- [x] plan committed first
- [x] reproduced the defect from this branch's base with the triage repro
      (`/home/shyft/tmp/teamsplus_lotek_notes/lotek_triage/repro/repro_checklist_ui.py`)
- [x] **measured that the issue's root cause is only half the story** (see Notes): mounted in lotek,
      `scribble.css` is never linked, so on production **no** `.ckp-*` rule applied at all
- [x] panel rules moved to their own `scribble/static/checklists_panel.css`, linked by the pages that
      render the panel through `head_extra` — works standalone AND mounted
- [x] `.ckp-status` themed off the same tokens as its row-mate `.ckp-note` (native arrow kept)
- [x] `.ckp-tray[hidden] { display: none; }` — the cause of the dashed empty box **when closed**
- [x] **(review round 1)** the tray no longer opens EMPTY either: with zero templates it now carries a
      `.ckp-tray-empty` line instead of an 18px dashed sliver — #44's second sub-item, properly closed
- [x] `ckp-tmpl` dropped from `checklists.js`
- [x] 14 new guards in `tests/test_checklists_panel_theming.py`, each watched fail (below)
- [x] **(review round 2)** the browser lane now drives the **MOUNTED** shell as well as the standalone
      one, so the row-layout guard measures the geometry in the shell that actually lost it
- [x] visual after-shots: standalone (dark + light), tray open/closed, MOUNTED, library page,
      **empty tray before/after** (`/home/shyft/tmp/ck44_repair/empty_tray.png`)

## Remaining
- [ ] **owed, not blocking:** one run of the mounted panel against **lotek's real `base.html` +
      `styles.css`** on the lotek side, at the extension-pin bump (the extensions CLAUDE.md rule "prove
      a mounted behaviour against lotek's suite too"). The round-2 reviewer did exactly this by hand and
      it passed, and this branch's new `mounted_panel_page` lane covers the panel-supplies-its-own-layout
      half — neither is the lotek-side run the rule asks for.
- [ ] nothing else for #44 — both of its sub-items are closed. Follow-ups filed in the hand-off report,
      not done here: `checklists_library.html` hardcodes scribble's own base instead of `scribble_base`
      (and moving it owes `.cklib-*` the same page-linked-stylesheet treatment `.ckp-*` just got — now
      guarded, see round 1), and `ghost` is an undefined class on two panels.

## What changed
| File | Change |
|---|---|
| `scribble/static/checklists_panel.css` | **new.** The panel's `.ckp-*`/`.ck-*` rules, with `.ckp-status` themed and `.ckp-tray[hidden]` added. |
| `scribble/static/scribble.css` | panel rules removed (pointer comment left; explains why they must not come back). |
| `scribble/templates/scribble/base.html` | defines `head_extra` in `<head>` — parity with cream/registrar/vector; scribble had no per-page head hook at all. |
| `scribble/templates/scribble/engagement.html` | links the panel stylesheet via `head_extra`. |
| `scribble/templates/scribble/checklists_library.html` | same link (it renders `.ckp-kind`). |
| `scribble/static/checklists.js` | `ckp-tmpl` dropped from the template buttons; **an empty tray now says why instead of opening blank** (round 1). |
| `scribble/tests/test_checklists_panel_theming.py` | **new.** 9 hermetic guards + 5 browser cases — the browser lane runs in BOTH shells (`panel_page` standalone, `mounted_panel_page` against a host-shaped base). |

## Red-then-green
Every guard was broken on purpose, run, and restored. Breakage script:
`/home/shyft/tmp/ck44_redgreen.sh` + `/home/shyft/tmp/ck44_redgreen2.sh`.

```text
A .ckp-status de-themed to the shipped one-liner
  RED  test_panel_stylesheet_themes_the_status_control
       AssertionError: .ckp-status declares no background: [' font-size: 12px; min-width: 92px; ']
  RED  test_status_select_matches_the_field_next_to_it        ← the client-visible one
       AssertionError: status control background rgb(239, 239, 239) != note rgb(13, 23, 34)

B .ckp-tray[hidden] deleted
  RED  test_panel_stylesheet_keeps_the_tray_hidden_when_hidden
       AssertionError: .ckp-tray sets display but has no .ckp-tray[hidden]{display:none} override
  RED  test_assign_tray_is_invisible_until_opened_and_closes_again
       AssertionError: hidden tray is still displayed / assert 'flex' == 'none'

C display:flex removed from .ckp-item
  RED  test_panel_rows_lay_out_beside_their_status_control
       AssertionError: item text is not to the right of the control:
       {'sel': {'x': 284, 'y': 672.65625, 'h': 25}, 'text': {'x': 284, 'y': 697.65625, 'h': 21}}

D engagement.html's head_extra block removed (the pre-fix state)
  RED  test_engagement_page_links_the_panel_stylesheet
       AssertionError: panel stylesheet not linked: ['<link ... href="/scribble/static/scribble.css" />']
  RED  test_mounted_page_links_the_panel_stylesheet   (same, with only the host's styles.css present)

E head_extra made to link scribble.css instead (the tempting "fix" — a shell hijack)
  RED  test_mounted_page_links_the_panel_stylesheet
       AssertionError: mounted page lost the panel stylesheet:
       ['<link ... "/static/styles.css" />', '<link ... "/scribble/static/scribble.css" />']

F .ckp-status re-declared in scribble.css (two homes again)
  RED  test_panel_rules_have_a_single_home
       AssertionError: panel classes re-declared in scribble.css: ['ckp-status']

G checklists_library.html's head_extra removed
  RED  test_checklists_library_page_links_the_panel_stylesheet
       AssertionError: panel stylesheet not linked: ['<link ... "/scribble/static/scribble.css" />']

H ckp-tmpl restored on the template buttons
  RED  test_every_panel_class_the_js_emits_has_a_css_rule
       AssertionError: classes emitted with no CSS rule: ['ckp-tmpl']

I checklists_panel.css renamed away
  RED  test_panel_stylesheet_is_served
       AssertionError: assert 404 == 200

GREEN, tree restored:  11 passed  (tests/test_checklists_panel_theming.py)
```

### Review round 1 (adversarial review, verdict REPAIR / 0 blocking / 2 CONCERN)

Both CONCERNs were **accepted and fixed** — nothing refuted. The reviewer independently reproduced
finding 1 through a supported UI action, and I reproduced it again from scratch before touching anything
(`/home/shyft/tmp/ck44_repair/empty_tray_repro.py`, shot `empty_tray.png`): 7 templates hidden ⇒
`TRAY: {display: 'flex', children: 0, text: '""', 890×18px, borderTopStyle: 'dashed'}` — #44's artifact,
on the "fixed" branch. After the repair the same probe reads
`{children: 1, text: "No checklist templates available — add or unhide one in the checklist library.",
890×36px}`.

1. **The tray could still open empty** (#44's second sub-item). `checklists.js` now appends a
   `.ckp-tray-empty` line when the groups loop produced nothing, before revealing the tray, so the
   operator learns *why* instead of reading the button as broken. New rule in `checklists_panel.css`.
2. **The `.cklib-*` comment in `scribble.css` stated a measured falsehood** ("unstyled on the host").
   Reworded to the measured truth in both the stylesheet and `checklists_library.html`, and the same
   sentence corrected in this plan's Notes and Remaining.

Two new guards, plus the existing drift guard's automatic coverage of the new class:

```text
J tray.hidden = false made unconditional again (the pre-round-1 state)
  RED  test_assign_tray_never_opens_as_an_empty_dashed_box
       AssertionError: tray opened with no content: {'display': 'flex', 'text': '', 'h': 18}
       assert ''
                                                       ← the reviewer's measured 890x18 dashed sliver

K checklists_library.html switched to `{% extends scribble_base %}` (the filed follow-up, i.e. the
  future change this guard exists to catch — NOT a hypothetical)
  RED  test_mounted_library_page_still_ships_its_cklib_rules
       AssertionError: the mounted library page renders classes no stylesheet it links defines:
       ['cklib-act', 'cklib-actions', 'cklib-card', 'cklib-card-head', 'cklib-count', 'cklib-err',
        'cklib-form', 'cklib-form-actions', 'cklib-form-row', 'cklib-head', 'cklib-showhidden',
        'cklib-tag'] — move them out of scribble.css into a page-linked sheet (ext#44 review)

L .ckp-tray-empty rule deleted from checklists_panel.css (proving the new class inherits the existing
  drift guard rather than being exempt from it)
  RED  test_every_panel_class_the_js_emits_has_a_css_rule
       AssertionError: classes emitted with no CSS rule: ['ckp-tray-empty']

GREEN, tree restored:  13 passed  (tests/test_checklists_panel_theming.py)
```

Fixture note: `panel_app`'s server/browser plumbing was extracted into `_boot`, `_serving` and a shared
module-scoped `browser` fixture so the new empty-tray case gets its OWN app and page — the original
board fixture is module-scoped and shared, so hiding its templates in place would have leaked state into
the other browser guards.

### Round-1 evidence (kept — the tool counts moved into the single `## Checks` block at the end)
```text
repro/repro_checklist_ui.py (the triage script)           → re-run, panel still themed, no dashed box
                                                            (/home/shyft/tmp/ck44_repair/repro_after/)
MOUNTED against lotek's REAL base.html + styles.css       → /home/shyft/tmp/ck44_repair/
  (mounted_empty_tray.py; the empty-tray fix where the       mounted_empty_tray.png. Links:
   client actually is, not just standalone)                  flatpickr, /static/styles.css,
                                                            /static/combobox.css,
                                                            /scribble/static/checklists_panel.css
                                                            (scribble.css correctly absent). Message
                                                            colour rgb(143,164,184) == the host's own
                                                            dark `--muted: #8fa4b8`, so the new rule
                                                            adds no token the host lacks.
```

### Review round 2 (adversarial review, verdict REPAIR / 0 blocking / 1 CONCERN)

**Accepted and fixed, not refuted.** The reviewer's finding was that
`test_panel_rows_lay_out_beside_their_status_control` **passed against the unfixed tree** — they
measured it by exporting `origin/main` and dropping the branch's test file in unchanged — so its
docstring ("the mounted panel lost `.ckp-item`'s flex layout entirely") described coverage the test did
not have. It drove `panel_page`, which boots `base_template="scribble/base.html"`, and in that shell
scribble.css always supplied `.ckp-item { display: flex }`. The layout was only ever lost MOUNTED. The
plan's breakage **C** reddened it only by deleting `display: flex`, which is not the bug.

They offered a docstring reword as the cheap close. Taking the other option instead — **a real mounted
browser lane** — because the reword would have left #44's actual failure mode measured by nothing:

- `_boot(..., mounted=True)` boots the same board against `_HOST_BASE` (the host-shaped base the
  hermetic tests already used) with `static_folder=None`, so `/static/styles.css` **404s**. Anything
  laid out in that shell was laid out by the panel's own stylesheet and by nothing else — deliberately
  harsher than prod, and it makes the attribution unarguable.
- `mounted_panel_app` / `mounted_panel_page` get their own app + sqlite, so no state crosses lanes.
- The guard is now `@pytest.mark.parametrize("shell", ["panel_page", "mounted_panel_page"])`, and its
  docstring says plainly that the two lanes are **not** equal evidence: standalone is a no-regression
  pin, mounted is the one that carries the defect.

The one thing this lane still cannot see is a HOST rule that *fights* the panel — `_HOST_BASE` is a
stand-in and serves no stylesheet. Said so in the module docstring rather than implying otherwise.

**The mounted lane asserts GEOMETRY only, on purpose — do not add a theming assertion to it.** With no
host stylesheet there are no `--field-bg`/`--ink` tokens, so `.ckp-status` and `.ckp-note` both fall
back to transparent and "the select matches its row-mate" would pass *vacuously*, in exactly the shell
where it would look most convincing. `display: flex` needs no tokens, which is why it is the property
this lane can honestly measure. Theming stays pinned standalone (where the tokens are real) plus the
by-hand mounted runs against lotek's actual `styles.css`. Synthesising lotek's tokens into the fixture
would only create a second, drifting copy of values that live in another repo.

```text
M the whole production tree reverted to origin/main (`git archive origin/main scribble`), this branch's
  test file copied in unchanged — i.e. the reviewer's own experiment, re-run against the repaired test

  GREEN test_panel_rows_lay_out_beside_their_status_control[panel_page]        ← the reviewer's point,
                                                                                 confirmed: standalone
                                                                                 never had the defect
  RED   test_panel_rows_lay_out_beside_their_status_control[mounted_panel_page]
        AssertionError: [mounted_panel_page] item text is not to the right of the control:
        {'sel': {'x': 8, 'y': 452.0625, 'h': 19}, 'text': {'x': 8, 'y': 471.0625, 'h': 18}}
        assert 8 > 8
                                        ← same x, 19px apart: the select stacked ABOVE its item text,
                                          the exact symptom the docstring names
  werkzeug log for that page: GET /static/styles.css → 404 (the host contributes nothing),
                              GET /scribble/static/checklists_panel.css → absent on main

  whole file against that unfixed tree:  12 failed, 2 passed
    the 2 that pass are correct: [panel_page] above, and
    test_mounted_library_page_still_ships_its_cklib_rules (it guards a reachability that HOLDS today —
    see the `.cklib-*` note below — and reddens only when the library page moves to `scribble_base`)

GREEN, tree restored:  14 passed  (tests/test_checklists_panel_theming.py)
```

Reviewer notes taken without a code change:
- **"the trailing `## Checks` block still carries the pre-round-1 numbers."** Correct — it did. The two
  blocks are now merged into the single current one below; there is no superseded copy left.
- **"the lotek-side pin bump still owes one look at the real thing"** (the extensions CLAUDE.md rule:
  prove a mounted behaviour against lotek's suite too). Still true and still owed — moved to Remaining.
  The reviewer did run it against lotek's real `base.html` + `styles.css` and it passed, and the new
  mounted lane narrows the gap, but neither is the lotek-side run this repo's rules ask for.

## Checks
Current as of review round 2. (This block used to repeat the pre-round-1 numbers alongside a newer
"re-run after round 1" block; the reviewer flagged the stale copy, so there is now exactly one.)
```text
uvx ruff check scribble/tests/test_checklists_panel_theming.py scribble
                                                          → All checks passed!
uv run --extra dev pyrefly check tests/test_checklists_panel_theming.py
                                                          → INFO 0 errors
uv run --extra dev pytest tests/test_checklists_panel_theming.py
                                                          → rc=0, 14 passed (was 13; +1 = the new
                                                            [mounted_panel_page] parametrisation)
uv run --extra dev pytest -q (whole scribble suite)        → rc=0. 625 tests collected (was 624):
                                                            623 passed, 2 skipped, 0 failed, 0 errors.
                                                            Both skips are the pre-existing pair in
                                                            tests/test_db_additive_migration.py,
                                                            "needs a real Postgres
                                                            (SCRIBBLE_TEST_PG_URL)".
                                                            (`addopts = "-q"` + `-q` suppresses the
                                                            summary line; counted from the progress
                                                            characters + `--collect-only`.)
red-then-green, this round                                → transcript M in "Review round 2" above:
                                                            the mounted lane fails on origin/main,
                                                            the standalone lane passes there.
```

## Notes / gotchas
- **The issue's root cause was incomplete, and the correction is the important part.** #44 says the
  defect is one missing rule because `.ckp-status` only declared `font-size`/`min-width`. True
  standalone. But `scribble.css` is linked by **scribble's own base template only**; mounted in lotek a
  page renders inside the HOST's `base.html`, whose only stylesheet hook is `head_extra` — and **no
  scribble template filled it** (`grep -rn head_extra scribble/` → zero hits before this branch, while
  cream/registrar/vector all have it). Measured with a host-shaped base + lotek's real `styles.css`:
  the mounted panel had **no** `.ckp-*` rule — rows unflexed (select stacked above its text), chips and
  kind pills as bare text (`coverageOpen: 10`), note fields native white. Prod is mounted, so a
  scribble.css-only fix would have "fixed" the repro and left the client's screen unchanged.
- **`.cklib-*` DOES reach the host today — it is not "stylesheet-only, unstyled on the host".**
  *(corrected in review round 1; the first pass asserted the opposite in a code comment and in its
  hand-off list.)* Measured: `.cklib-*`'s only consumer is `checklists_library.html`, which hardcodes
  `{% extends "scribble/base.html" %}`, so that page renders in scribble's own shell — and links
  scribble.css — **even when mounted**. Mounted stylesheet links on `/scribble/checklists` are
  `['/scribble/static/scribble.css', '/scribble/static/checklists_panel.css']`. So the rules arrive;
  what they depend on is that hardcode. Switching the page to `scribble_base` (the filed follow-up) is
  what breaks them, and `test_mounted_library_page_still_ships_its_cklib_rules` now fails at exactly
  that moment, naming all 12 classes that would go unstyled.
- **Do not link `scribble.css` from a host page.** It redefines `:root`, `body`, `.card`, `.btn`,
  `.table` and the sidebar/appbar — injecting it into lotek would silently restyle the host shell on
  every scribble page. That is why the panel got its own file containing panel classes only, and why
  breakage E above is a guarded failure rather than a shortcut.
- **The dashed empty rectangle had TWO causes, and the first pass only fixed one.** *(corrected in
  review round 1 — the original wording of this bullet, "the dashed rectangle was not 'the tray renders
  when empty'", was true of the instance I observed and false as a general claim.)* Cause 1, the one the
  repro showed: the tray already ships `hidden` and the JS toggles that attribute, but an author
  `display: flex` outranks the UA's `[hidden] { display: none }` — so the CLOSED tray painted, and the
  assign button never visually closed it. Cause 2, which #44 named and I wrongly dismissed:
  `tray.hidden = false` ran unconditionally after the groups loop, so an OPEN tray with zero children
  painted the identical box. Reachable through the UI (`suggest` filters `hidden`/`inactive`, so the
  library page's Hide button on every template empties both lists — measured 890×18px, `children: 0`,
  `innerText: ""`). Both are fixed now: `[hidden]` for closed, an explanatory `.ckp-tray-empty` line for
  open-but-empty.
- **`.ckp-status` is themed, not `.input`-ified.** The issue suggested adding the host's `.input`
  class. Rejected on measurement: `.input` sets `width: 100%` (would blow out the flex row), and it
  does not exist in scribble.css at all, so standalone would stay native. Token-based theming tracks
  `data-theme` just as well — verified in both themes (dark `rgb(13,23,34)`/`rgb(220,231,243)`, light
  `rgb(255,255,255)`/`rgb(15,23,32)`, each identical to the row's note field).
- Screenshots: before `evidence/checklist_panel.png` (and `/home/shyft/tmp/ck44_before/`), after
  `/home/shyft/tmp/ck44_after/` — `checklist_panel.png`, `ckp_light.png`, `ckp_tray_open.png`,
  `ckp_tray_closed.png`, `ckp_mounted_after.png` (select fixed, panel still unstyled — the interim
  state), `ckp_mounted_fixed.png` (the delivered state), `ckp_library.png`.
- Repro/probe scripts used, kept outside the repo: `/home/shyft/tmp/ck44_shots.py`,
  `ck44_mounted_probe.py`, `ck44_mounted_shot.py`, `ck44_lib_shot.py`.
- **The browser guards drive BOTH shells** *(corrected in review round 2 — this bullet used to say the
  standalone shell was "the only one this repo can boot", which was simply wrong: the same host-shaped
  base the hermetic tests use boots fine under a real server + Chromium).* `panel_page` is standalone;
  `mounted_panel_page` renders the same board inside `_HOST_BASE` with no host stylesheet at all, which
  is where the row-layout guard now measures. What is still NOT covered anywhere in this repo is a host
  rule *fighting* the panel — `_HOST_BASE` is a stand-in — so the genuine mounted rendering still wants
  one look in lotek after the pin bump.
