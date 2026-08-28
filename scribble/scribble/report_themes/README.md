# Bundled report Themes

This directory carries Scribble's **bundled** report Themes (see `CONTEXT.md`'s **Provenance**: this
is the "ships inside Scribble" case, as distinct from an *installed* Theme package or an *override* an
operator supplies at runtime) as data — one `<name>.toml` file per Theme, plus whatever `.woff2` font
files those Theme files declare. Loaded by `scribble.reporting.theme_files`; see that module's
docstring for the loader API, the wheel-packaging note, and why fonts are embedded rather than linked.

A Theme is *appearance only* — palette, typefaces, marks. It never adds, removes, or reorders a report
Block; that is a Layout concern (`reporting/layouts.py`). Use the vocabulary in `CONTEXT.md` exactly:
this file is a **Theme**, never a "template".

## File naming

`<name>.toml` where `<name>` is the Theme's slug and MUST equal `[identity].name` inside the file —
`load_theme_file` rejects a mismatch. `light.toml` and `dark.toml` ship today.

## Schema

```toml
[identity]
name = "light"      # required, non-empty, MUST match the filename (sans .toml)
label = "Light"      # required, non-empty — display text in the Theme switcher

[tokens]
# Required. A FLAT string -> string mapping — no nested tables, no non-string values. Every key here
# becomes one design token (a colour, a type stack, a radius — see CONTEXT.md's Token definition), and
# the WHOLE table is run through `reporting.tokens.validate_tokens` (#101): an unknown key, or any
# single invalid value, rejects the ENTIRE Theme file — the same wholesale-reject rule that module
# applies to an operator-supplied `override` Theme, because a bundled file that typos a colour is no
# more trustworthy than one an attacker crafted. See `reporting/tokens.py` for the exact grammar each
# Token kind accepts (colours: strict `#rgb`/`#rrggbb`/`#rrggbbaa` hex only; dimensions: a bounded
# number plus `px`/`rem`/`em`/`ch`/`%`; font stacks: a restricted charset, no token wired to real CSS
# yet). Convention: name a token after the CSS custom property it feeds, minus the leading `--` (e.g.
# the `--sev-critical` custom property <-> the `sev-critical` token) — see light.toml / dark.toml,
# which are a straight port of `render_html`'s stylesheet under that rule.
bg = "#f6f8fa"
sev-critical = "#b3261e"
# … etc. — the full set this loader currently accepts is `reporting.tokens.ALLOWED_TOKENS`.

[fonts]
# Optional table. `embed` (bool, default false) gates whether `build_font_face_css` emits ANYTHING
# for this Theme at all — see `theme_files.py` for why embedding (not a <link>) is the only supported
# mechanism for a report. A Theme may declare faces with `embed = false` (e.g. while sourcing fonts)
# without anything rendering yet.
embed = false

[[fonts.face]]
# Zero or more of these. Each is one @font-face this Theme wants embedded, once `embed = true` and the
# named file actually exists (see "Font files" below).
family = "Inter"                 # required, non-empty — the CSS font-family name
weight = 400                     # required — int (400, 700, …) or CSS keyword string ("bold")
style = "normal"                 # required, non-empty — "normal" | "italic" | …
file = "inter-regular.woff2"     # required — a BARE filename, sibling of this .toml, ending .woff2.
                                  # No path separators and no "..": it is resolved inside THIS
                                  # directory only, never as a filesystem path. Anything else is
                                  # rejected at parse time (ThemeFileError), not silently ignored.

[marks]
# Reserved placeholder for #104 (logo/shape assets — see CONTEXT.md's Mark definition). The section
# may be present-and-empty (as in light.toml / dark.toml) or omitted entirely; nothing under it is
# read by this loader yet. Do not rely on any key here surviving #104's schema.
```

## Font files

No `.woff2` files are committed in this directory yet — `light.toml` and `dark.toml` both ship with
`embed = false` and no declared faces, matching the system-font stack the shipped stylesheet already
uses. When a Theme starts shipping a real web font:

1. Drop the `.woff2` file(s) directly in this directory (a sibling of the Theme's `.toml` — never a
   subdirectory; `theme_files._is_safe_sibling_filename` rejects anything else).
2. Add one `[[fonts.face]]` table per family/weight/style combination, naming that exact filename.
3. Flip `embed = true` once you want it to actually render.

`build_font_face_css` degrades cleanly at every stage of that rollout: a declared-but-missing file is
skipped (not an error), and the whole feature is a no-op while `embed` stays `false`. It also enforces
a total per-Theme size ceiling (`theme_files.MAX_EMBEDDED_FONT_BYTES`) across every embedded face, so
one Theme cannot make every report balloon in size — a face that would exceed the remaining budget is
skipped, never partially emitted.

## Adding a new bundled Theme

1. Copy `light.toml` (or `dark.toml`) to `<name>.toml` in this directory.
2. Set `[identity].name` to `<name>` exactly, and a human-readable `[identity].label`.
3. Fill in `[tokens]` — every key must be one of `reporting.tokens.ALLOWED_TOKENS` and pass that
   token's grammar, or `load_theme_file` raises `ThemeFileError` for the whole file.
4. **Add a matching entry to `reporting/themes.py`'s `_THEMES` registry.** This step is REQUIRED and
   is easy to miss, because every earlier step appears to succeed without it: `list_theme_files()`
   sees the new file and `load_theme_file()` parses it, but `themes.get_theme("<name>")` consults the
   hardcoded registry, does not find it, and **silently falls back to `auto`** — so the Theme never
   appears in the switcher and the report renders with no override at all. Nothing raises and nothing
   is logged. (`list_theme_files()` has no production caller today; it exists for the tests and for
   whoever replaces the registry with real discovery.)
5. No `pyproject.toml` change is needed — this directory already ships inside the wheel automatically
   (see `theme_files.py`'s module docstring for how that was verified).

### Why the registry is hardcoded, and what it costs

Reading the bundled Themes out of this directory automatically is the obvious improvement, and it is
deliberately NOT done here. Making a new Theme reachable immediately makes an **operator-authored**
Theme reachable, and this module's cascade is not yet ready for one: the screen override is emitted at
`:root:root` (0-2-0, later in the sheet), which beats the dark palette as well as the light one, and
`validate_tokens` explicitly permits a PARTIAL token set. So a brand Theme that sets only, say,
`accent-ink` — a colour chosen against white — would paint it over the dark palette on a dark-mode
screen. That is the same defect class as the print bug `@media print` exists to prevent, reintroduced
on the other medium.

Fixing that needs a decision this branch does not make: either scope the screen override away from
dark (`:root:root:not([data-theme="dark"])`) and add a `[dark_tokens]` opt-in mirroring
`[print_tokens]`, or require a colour token set to be all-or-nothing. Until then the registry stays
closed, the bundled `light`/`dark` Themes are the only reachable ones, and the risk stays theoretical.
Installed/discovered Themes and operator overrides land with that decision — see ext#113.
