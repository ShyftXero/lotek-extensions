"""Bundled report Theme files — TOML plus their (absent-today) web fonts (#102).

Covers `scribble.reporting.theme_files`:

- the schema round-trips a hand-built minimal Theme file through the pure parser,
- `light.toml` and `dark.toml` both load, and their `[tokens]` values MATCH `render_html`'s `_CSS`
  verbatim — the anti-drift test: these two files are a REFACTOR of the stylesheet, not a redesign,
  so any future edit to either without the other fails here,
- a missing font file degrades cleanly (the normal state today — no `.woff2` is committed yet),
- a face that would blow the total embedding budget is skipped, never partially emitted,
- malformed TOML, an unknown theme name, a name/filename mismatch, and a `[tokens]` payload that fails
  `reporting.tokens`'s closed allowlist all reject — consistently: unknown name -> ``None``, a broken
  KNOWN file -> ``ThemeFileError``, never a partial ``ThemeFile``,
- the `importlib.resources` path used to reach the two bundled files actually works.
"""

from __future__ import annotations

import re

import pytest

from scribble.reporting import theme_files
from scribble.reporting.render_html import _CSS
from scribble.reporting.theme_files import (
    FontFace,
    ThemeFile,
    ThemeFileError,
    build_font_face_css,
    font_data_uri,
    list_theme_files,
    load_theme_file,
)

_MINIMAL_TOML = """
[identity]
name = "sample"
label = "Sample"

[tokens]
bg = "#112233"
radius = "8px"

[fonts]
embed = false

[marks]
"""


# --- schema round-trip -----------------------------------------------------------------------------

def test_minimal_schema_round_trips():
    theme = theme_files._parse_theme_toml("sample", _MINIMAL_TOML)
    assert theme == ThemeFile(
        name="sample",
        label="Sample",
        tokens={"bg": "#112233", "radius": "8px"},
        embed_fonts=False,
        faces=(),
    )


def test_schema_round_trips_with_declared_faces():
    text = _MINIMAL_TOML.replace(
        "[fonts]\nembed = false",
        '[fonts]\nembed = true\n\n[[fonts.face]]\nfamily = "Inter"\nweight = 400\n'
        'style = "normal"\nfile = "inter-regular.woff2"',
    )
    theme = theme_files._parse_theme_toml("sample", text)
    assert theme.embed_fonts is True
    assert theme.faces == (
        FontFace(family="Inter", weight=400, style="normal", file="inter-regular.woff2"),
    )


def test_marks_section_may_be_entirely_absent():
    """`[marks]` is a placeholder (#104) — a file that never mentions it at all is still legal."""
    text = _MINIMAL_TOML.replace("\n[marks]\n", "\n")
    theme = theme_files._parse_theme_toml("sample", text)
    assert theme.name == "sample"


def test_fonts_section_may_be_entirely_absent():
    text = _MINIMAL_TOML.replace("[fonts]\nembed = false\n\n", "")
    theme = theme_files._parse_theme_toml("sample", text)
    assert theme.embed_fonts is False
    assert theme.faces == ()


# --- light.toml / dark.toml load, and match render_html's _CSS verbatim ----------------------------

def test_list_theme_files_lists_both_bundled_themes():
    assert list_theme_files() == ["dark", "light"]


@pytest.mark.parametrize("name", ["light", "dark"])
def test_bundled_theme_loads(name):
    theme = load_theme_file(name)
    assert theme is not None
    assert theme.name == name
    assert theme.embed_fonts is False
    assert theme.faces == ()


def _root_block_tokens(css: str, anchor: str) -> dict[str, str]:
    """Extract every ``--name: value;`` pair from the CSS rule starting at ``anchor``.

    Brace-counted (not a fixed-width slice) so this survives cosmetic reformatting of the stylesheet;
    none of Scribble's palette blocks nest braces inside themselves, so a simple counter is exact here.
    """
    start = css.index(anchor)
    open_brace = css.index("{", start)
    depth = 0
    i = open_brace
    while True:
        if css[i] == "{":
            depth += 1
        elif css[i] == "}":
            depth -= 1
            if depth == 0:
                break
        i += 1
    body = css[open_brace + 1 : i]
    return {k: v.strip() for k, v in re.findall(r"--([\w-]+):\s*([^;]+);", body)}


def test_light_theme_tokens_match_render_html_css_root_block_exactly():
    """The anti-drift test: every value in ``light.toml`` was copied out of ``_CSS``'s ``:root {}``
    block by hand for this ticket — this re-parses that same block and diffs the two, so a future edit
    to either the stylesheet or the Theme file alone (instead of both) fails immediately."""
    expected = _root_block_tokens(_CSS, ":root {")
    theme = load_theme_file("light")
    assert theme is not None
    assert theme.tokens == expected


def test_dark_theme_tokens_match_the_effective_dark_palette():
    """`dark.toml` must equal the FULL effective palette under the dark stamp, not just the block that
    literally says ``[data-theme="dark"]``: `render_html`'s dark blocks only override the sixteen
    colour tokens (see `reporting/themes.py`'s module docstring) — ``radius``/``measure``/``maxw`` are
    declared once, in the light ``:root {}``, and are never re-themed, so the EFFECTIVE dark value for
    those three is whatever ``:root`` sets. This is exactly why `dark.toml` carries all nineteen keys
    rather than only the sixteen the dark CSS block itself redeclares — see its own header comment."""
    light_root = _root_block_tokens(_CSS, ":root {")
    dark_overrides = _root_block_tokens(_CSS, '\n:root[data-theme="dark"] {')
    # The override block should be a strict subset naming only the re-themed colour tokens — if this
    # ever fails, `_CSS` started re-theming a dimension too and `dark.toml` needs a real update, not
    # just this test's assumption about which keys merge from `light_root`.
    assert set(dark_overrides) < set(light_root)
    theme = load_theme_file("dark")
    assert theme is not None
    assert theme.tokens == {**light_root, **dark_overrides}


def test_dark_theme_tokens_also_match_the_prefers_color_scheme_twin():
    """`:root:not([data-theme="light"]) { @media (prefers-color-scheme: dark) { … } }` is documented in
    `reporting/themes.py` as carrying the IDENTICAL values to the `[data-theme="dark"]` stamp block —
    prove that rather than assume it, since `dark.toml` is checked against only one of the two above."""
    light_root = _root_block_tokens(_CSS, ":root {")
    outer_start = _CSS.index(':root:not([data-theme="light"]) {')
    inner_at = _CSS.index("@media", outer_start)
    media_overrides = _root_block_tokens(_CSS[inner_at:], "{")
    theme = load_theme_file("dark")
    assert theme is not None
    assert theme.tokens == {**light_root, **media_overrides}


# --- font embedding: absence degrades cleanly, budget is enforced ----------------------------------

def test_absent_font_file_is_the_normal_state_today():
    """No `.woff2` ships in `report_themes/` yet — both bundled Themes must reflect that."""
    for name in ("light", "dark"):
        theme = load_theme_file(name)
        assert theme is not None
        assert build_font_face_css(theme) == ""


def test_font_data_uri_returns_none_for_a_missing_file():
    face = FontFace(family="Inter", weight=400, style="normal", file="does-not-exist.woff2")
    assert font_data_uri(face) is None


def test_build_font_face_css_is_a_noop_when_embed_fonts_is_false():
    face = FontFace(family="Inter", weight=400, style="normal", file="does-not-exist.woff2")
    theme = ThemeFile(name="t", label="T", tokens={}, embed_fonts=False, faces=(face,))
    assert build_font_face_css(theme) == ""


def test_build_font_face_css_skips_a_missing_face_even_when_embedding_is_on():
    face = FontFace(family="Inter", weight=400, style="normal", file="does-not-exist.woff2")
    theme = ThemeFile(name="t", label="T", tokens={}, embed_fonts=True, faces=(face,))
    assert build_font_face_css(theme) == ""


def test_build_font_face_css_embeds_a_present_face(monkeypatch):
    """No real `.woff2` is committed (see the module docstring), so this proves the SUCCESS path by
    faking the byte source at the seam `theme_files._read_font_bytes` — the smallest surface that
    stands between "file exists" and "CSS text comes out"."""
    face = FontFace(family="Inter", weight=400, style="normal", file="inter.woff2")
    theme = ThemeFile(name="t", label="T", tokens={}, embed_fonts=True, faces=(face,))
    monkeypatch.setattr(
        theme_files,
        "_read_font_bytes",
        lambda f, pkg=None: b"FAKEFONTBYTES" if f is face else None,
    )
    css = build_font_face_css(theme)
    assert "@font-face" in css
    assert 'font-family: "Inter"' in css
    assert "font-weight: 400" in css
    assert "data:font/woff2;base64," in css


def test_build_font_face_css_skips_a_face_that_would_exceed_the_total_budget(monkeypatch):
    too_big = FontFace(family="Huge", weight=400, style="normal", file="huge.woff2")
    fits = FontFace(family="Small", weight=700, style="italic", file="small.woff2")
    theme = ThemeFile(name="t", label="T", tokens={}, embed_fonts=True, faces=(too_big, fits))

    def fake_read(face: FontFace, package: str | None = None) -> bytes | None:
        if face is too_big:
            return b"x" * (theme_files.MAX_EMBEDDED_FONT_BYTES + 1)
        if face is fits:
            return b"y" * 10
        return None

    monkeypatch.setattr(theme_files, "_read_font_bytes", fake_read)
    css = build_font_face_css(theme)
    assert "Huge" not in css
    assert 'font-family: "Small"' in css


def test_font_data_uri_rejects_a_single_face_over_the_ceiling(monkeypatch):
    face = FontFace(family="Huge", weight=400, style="normal", file="huge.woff2")
    monkeypatch.setattr(
        theme_files, "_read_font_bytes", lambda f, pkg=None: b"x" * (theme_files.MAX_EMBEDDED_FONT_BYTES + 1)
    )
    assert font_data_uri(face) is None


# --- malformed / unknown / mismatched theme files reject, consistently -----------------------------

def test_malformed_toml_syntax_raises():
    with pytest.raises(ThemeFileError):
        theme_files._parse_theme_toml("broken", "this is [not valid toml")


def test_missing_identity_section_raises():
    with pytest.raises(ThemeFileError):
        theme_files._parse_theme_toml("x", "[tokens]\nbg = \"#112233\"\n")


def test_identity_name_must_match_the_filename():
    text = _MINIMAL_TOML.replace('name = "sample"', 'name = "other"')
    with pytest.raises(ThemeFileError):
        theme_files._parse_theme_toml("sample", text)


def test_unknown_token_name_rejects_the_whole_file():
    text = _MINIMAL_TOML.replace("radius = \"8px\"", 'radius = "8px"\nnot-a-real-token = "x"')
    with pytest.raises(ThemeFileError):
        theme_files._parse_theme_toml("sample", text)


def test_a_single_bad_token_value_rejects_the_whole_file_not_a_partial_theme():
    text = _MINIMAL_TOML.replace('bg = "#112233"', "bg = \"red\"")  # named colour, not hex
    with pytest.raises(ThemeFileError):
        theme_files._parse_theme_toml("sample", text)


def test_non_string_token_value_rejects():
    text = _MINIMAL_TOML.replace('radius = "8px"', "radius = 8")
    with pytest.raises(ThemeFileError):
        theme_files._parse_theme_toml("sample", text)


@pytest.mark.parametrize(
    "bad_file",
    ["../evil.woff2", "sub/evil.woff2", "..", "no-extension", "evil.ttf"],
)
def test_unsafe_or_wrong_extension_font_filename_rejects(bad_file):
    text = _MINIMAL_TOML.replace(
        "[fonts]\nembed = false",
        f'[fonts]\nembed = true\n\n[[fonts.face]]\nfamily = "Inter"\nweight = 400\n'
        f'style = "normal"\nfile = "{bad_file}"',
    )
    with pytest.raises(ThemeFileError):
        theme_files._parse_theme_toml("sample", text)


def test_backslash_in_font_filename_rejects():
    """A TOML LITERAL string (single-quoted) has no escape sequences, so this is the one way to get a
    genuine backslash character to `_is_safe_sibling_filename` without first tripping TOML's OWN
    basic-string escape parser (which would reject `"sub\\evil.woff2"` before this loader ever ran)."""
    text = _MINIMAL_TOML.replace(
        "[fonts]\nembed = false",
        "[fonts]\nembed = true\n\n[[fonts.face]]\nfamily = 'Inter'\nweight = 400\n"
        "style = 'normal'\nfile = 'sub\\evil.woff2'",
    )
    with pytest.raises(ThemeFileError):
        theme_files._parse_theme_toml("sample", text)


@pytest.mark.parametrize(
    "bad_family",
    [
        "Evil'; } body { display:none",  # quote/brace/colon punctuation the CSS charset excludes
        "",  # empty
        "x" * 100,  # over the length ceiling
    ],
)
def test_font_family_charset_rejects_css_breakout_attempts(bad_family):
    """These are all syntactically legal TOML strings — the point is `_valid_family`'s charset/length
    check rejects them, not TOML's own parser (which would mask the check this test is for)."""
    text = _MINIMAL_TOML.replace(
        "[fonts]\nembed = false",
        f'[fonts]\nembed = true\n\n[[fonts.face]]\nfamily = "{bad_family}"\nweight = 400\n'
        'style = "normal"\nfile = "inter.woff2"',
    )
    with pytest.raises(ThemeFileError):
        theme_files._parse_theme_toml("sample", text)


@pytest.mark.parametrize("bad_weight", [0, 1001, -1, "heavy", "400.5"])
def test_font_weight_out_of_grammar_rejects(bad_weight):
    weight_literal = f'"{bad_weight}"' if isinstance(bad_weight, str) else str(bad_weight)
    text = _MINIMAL_TOML.replace(
        "[fonts]\nembed = false",
        f'[fonts]\nembed = true\n\n[[fonts.face]]\nfamily = "Inter"\nweight = {weight_literal}\n'
        'style = "normal"\nfile = "inter.woff2"',
    )
    with pytest.raises(ThemeFileError):
        theme_files._parse_theme_toml("sample", text)


def test_font_style_outside_the_closed_set_rejects():
    text = _MINIMAL_TOML.replace(
        "[fonts]\nembed = false",
        '[fonts]\nembed = true\n\n[[fonts.face]]\nfamily = "Inter"\nweight = 400\n'
        'style = "underline"\nfile = "inter.woff2"',
    )
    with pytest.raises(ThemeFileError):
        theme_files._parse_theme_toml("sample", text)


def test_unknown_theme_name_returns_none():
    assert load_theme_file("does-not-exist") is None


@pytest.mark.parametrize("name", ["", "../etc/passwd", "a/b", "a\\b", ".", ".."])
def test_unsafe_or_blank_theme_names_return_none_rather_than_touch_the_filesystem(name):
    assert load_theme_file(name) is None


# --- importlib.resources plumbing actually works ----------------------------------------------------

def test_importlib_resources_path_is_what_load_theme_file_actually_uses():
    """Not just "load_theme_file works" (covered above) — this pins that it is doing so via
    `importlib.resources` against the real installed package, the same API surface a zipimport wheel
    uses, per the module docstring's force-include / zipimport discussion."""
    from importlib import resources

    root = resources.files("scribble.report_themes")
    names = sorted(e.name.removesuffix(".toml") for e in root.iterdir() if e.name.endswith(".toml"))
    assert names == list_theme_files()
    assert (root / "light.toml").is_file()
    assert (root / "dark.toml").is_file()


@pytest.mark.parametrize(("name", "expected"), [("light", "light"), ("dark", "dark")])
def test_a_bundled_themes_declared_stamp_matches_the_registry(name, expected):
    """Two sources of truth for the same fact, so pin them together. `reporting/themes.py`'s registry
    is what actually stamps `<html data-theme>` for a bundled Theme; the `.toml` declares the same
    thing for the benefit of an INSTALLED Theme, which has no registry entry. If they ever disagree,
    a Theme's file says one palette and the page says another."""
    from scribble.reporting.themes import THEMES

    theme = load_theme_file(name)
    assert theme is not None
    assert theme.stamp == expected == THEMES[name].stamp


def test_an_unknown_stamp_is_refused():
    with pytest.raises(ThemeFileError):
        theme_files._parse_theme_toml(
            "sample", _MINIMAL_TOML.replace('label = "Sample"', 'label = "Sample"\nstamp = "chartreuse"')
        )


def test_stamp_defaults_to_following_the_viewer():
    """A Theme that says nothing gets "" — auto. Correct only for a Theme that re-themes BOTH palettes;
    a light-tuned brand should say `light` (see `_parse_theme_toml`'s note on the partial-set mix)."""
    assert theme_files._parse_theme_toml("sample", _MINIMAL_TOML).stamp == ""


# --- schema additions for INSTALLED Themes ---------------------------------------------------------
#
# A bundled Theme is a file in this package. An installed Theme arrives as TOML TEXT from another
# distribution's entry point, with no filesystem identity of its own — which breaks two assumptions
# the schema quietly made, and needs a third field for a logo. These pin all three.


def _with(section: str, body: str) -> str:
    """_MINIMAL_TOML with one section replaced/appended."""
    return _MINIMAL_TOML.replace(f"[{section}]", f"[{section}]\n{body}", 1)


def test_font_package_defaults_to_scribbles_own_for_a_bundled_theme():
    theme = theme_files._parse_theme_toml("sample", _MINIMAL_TOML)
    assert theme.font_package == "scribble.report_themes"


def test_an_installed_theme_may_name_its_own_font_package():
    """The gap this closes is silent: without it, `build_font_face_css` looks for an installed Theme's
    faces inside `scribble.report_themes`, finds nothing, and returns "" — a brand that renders in the
    right colours with the wrong typeface, and no error anywhere."""
    text = _with("fonts", 'package = "somepkg.fonts"')
    theme = theme_files._parse_theme_toml("sample", text)
    assert theme.font_package == "somepkg.fonts"


@pytest.mark.parametrize(
    "bad",
    ["../evil", "/abs/path", "pkg/sub", "pkg-with-dash", "1leading", "", "pkg..dup", "pkg."],
)
def test_a_font_package_that_is_not_a_dotted_identifier_is_refused(bad):
    """`package` names a package and must never be able to express a path. Combined with
    `_is_safe_sibling_filename` on each face, a Theme can reach exactly one directory: its own."""
    with pytest.raises(ThemeFileError):
        theme_files._parse_theme_toml("sample", _with("fonts", f'package = "{bad}"'))


def test_a_bad_font_package_degrades_rather_than_raising_at_read_time():
    """A package name that passes the grammar but is not importable must still not 500 a report —
    `resources.files` raises ModuleNotFoundError, which is not an OSError."""
    face = FontFace(family="Inter", weight=400, style="normal", file="x.woff2")
    assert theme_files._read_font_bytes(face, "no.such.package.exists") is None
    theme = ThemeFile(
        name="t", label="T", tokens={}, embed_fonts=True, faces=(face,),
        font_package="no.such.package.exists",
    )
    assert build_font_face_css(theme) == ""


@pytest.mark.parametrize("weight", ["300 700", "100 900", "400 400"])
def test_a_variable_font_may_declare_a_weight_range(weight):
    """Google serves Heebo and Montserrat as ONE file spanning the whole weight axis. Without ranges a
    Theme declares one face per weight pointing at the same file and pays the base64 cost per
    declaration — six files and 181 KB where three and 83 KB will do."""
    text = _with(
        "fonts",
        f'embed = true\n\n[[fonts.face]]\nfamily = "Heebo"\nweight = "{weight}"\n'
        'style = "normal"\nfile = "heebo.woff2"',
    )
    theme = theme_files._parse_theme_toml("sample", text)
    assert theme.faces[0].weight == weight


@pytest.mark.parametrize(
    "weight",
    ["700 300", "0 700", "300 1001", "300  700", "300,700", "a b", "300 700 900"],
)
def test_a_malformed_or_descending_weight_range_is_refused(weight):
    """Ascending, two values, both in range. `700 300` is the interesting one: CSS reads it as a range
    and a descending pair is not a range at all."""
    text = _with(
        "fonts",
        f'embed = true\n\n[[fonts.face]]\nfamily = "Heebo"\nweight = "{weight}"\n'
        'style = "normal"\nfile = "heebo.woff2"',
    )
    with pytest.raises(ThemeFileError):
        theme_files._parse_theme_toml("sample", text)


def test_a_logo_svg_is_read_as_source_text():
    text = _with("marks", "logo_svg = '<svg viewBox=\"0 0 1 1\"><path d=\"M0 0\"/></svg>'")
    theme = theme_files._parse_theme_toml("sample", text)
    assert theme.logo_svg is not None
    assert theme.logo_svg.startswith("<svg")


def test_a_theme_without_marks_has_no_logo():
    assert theme_files._parse_theme_toml("sample", _MINIMAL_TOML).logo_svg is None


def test_a_non_string_logo_is_refused():
    with pytest.raises(ThemeFileError):
        theme_files._parse_theme_toml("sample", _with("marks", "logo_svg = 42"))


def test_an_oversize_logo_is_refused_before_it_reaches_a_parser():
    """Bounded at the door. An unbounded blob here would be parsed, sanitized, and then inlined into a
    document — the cheapest place to refuse it is before any of that."""
    huge = "<svg>" + ("x" * (theme_files._MAX_LOGO_SVG_CHARS + 1)) + "</svg>"
    with pytest.raises(ThemeFileError):
        theme_files._parse_theme_toml("sample", _with("marks", f"logo_svg = '''{huge}'''"))


def test_the_logo_is_NOT_sanitized_here():
    """Deliberate, and worth pinning so nobody "hardens" it by adding a second opinion. Vetting lives
    in `reporting.marks.resolve_mark`, which keys off Provenance and is called by BOTH the write path
    and the render path. cream shipped a real bug by having its API and its renderer disagree about
    what was acceptable; one gate called from both sides is how that is not repeated."""
    hostile = "<svg><script>alert(1)</script></svg>"
    theme = theme_files._parse_theme_toml("sample", _with("marks", f"logo_svg = '{hostile}'"))
    assert theme.logo_svg == hostile  # stored verbatim; the gate is elsewhere, by design

# --- [fonts].package is honoured only for provenances that are already CODE -------------------------
#
# Security review of this branch proved, by execution, that `importlib.resources.files(name)` IMPORTS
# the named module. `[fonts].package` is operator-supplied TOML, so honouring it for an `override`
# Theme turned "edit branding" into "cause an arbitrary module import in the server process" --
# triggerable afterwards by ANY user who can view a report, since the font read happens during render
# and is not admin-gated, and silently, because that read swallows every exception.


def _themed_with_package(pkg: str) -> ThemeFile:
    face = FontFace(family="Acme", weight=400, style="normal", file="acme.woff2")
    return ThemeFile(
        name="acme", label="Acme", tokens={}, embed_fonts=True, faces=(face,), font_package=pkg
    )


@pytest.mark.parametrize("provenance", ["bundled", "installed"])
def test_a_code_provenance_may_name_its_own_font_package(provenance, monkeypatch):
    """A bundled Theme ships in this wheel; an installed one had to be pip-installed AND imported for
    its entry point to resolve at all. Naming a package it could already import grants it nothing."""
    seen: list[str] = []

    def spy(face, package=theme_files._PACKAGE):
        seen.append(package)
        return None

    monkeypatch.setattr(theme_files, "_read_font_bytes", spy)
    build_font_face_css(_themed_with_package("some.installed.fonts"), provenance=provenance)
    assert seen == ["some.installed.fonts"]


def test_an_override_theme_cannot_choose_the_font_package(monkeypatch):
    """The positive control. Remove the provenance check in `build_font_face_css` and this goes red:
    the operator-declared package would be passed through to `importlib.resources.files`, importing it.
    """
    seen: list[str] = []

    def spy(face, package=theme_files._PACKAGE):
        seen.append(package)
        return None

    monkeypatch.setattr(theme_files, "_read_font_bytes", spy)
    build_font_face_css(_themed_with_package("evil_side_effects"), provenance="override")
    assert seen == [theme_files._PACKAGE], "an override Theme must resolve inside scribble's package"
    assert "evil_side_effects" not in seen


def test_an_unknown_provenance_also_refuses_a_declared_package(monkeypatch):
    """Allowlist, not blocklist -- a typo'd or future provenance gets the safe branch, matching how
    `marks._SVG_ALLOWED_PROVENANCES` behaves for the same reason."""
    seen: list[str] = []

    def spy(face, package=theme_files._PACKAGE):
        seen.append(package)
        return None

    monkeypatch.setattr(theme_files, "_read_font_bytes", spy)
    build_font_face_css(_themed_with_package("whatever"), provenance="something-new")
    assert seen == [theme_files._PACKAGE]


def test_the_two_provenance_allowlists_agree():
    """`[fonts].package` and an SVG Mark are the two capabilities keyed off Provenance, and they must
    not drift apart -- "may name a package" and "may carry SVG" are the same trust question."""
    from scribble.reporting.marks import _SVG_ALLOWED_PROVENANCES

    assert theme_files._PACKAGE_ALLOWED_PROVENANCES == _SVG_ALLOWED_PROVENANCES
