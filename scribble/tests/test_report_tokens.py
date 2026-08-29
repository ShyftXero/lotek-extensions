"""Report Theme Tokens (#101): the closed allowlist, its per-kind validators, and CSS emission.

This is the security boundary of the whole Theme feature (see the module docstring on
``scribble.reporting.tokens``): the payload validated here eventually arrives as admin-typed text and
later as an uploaded bundle, and is injected into a document that also embeds client evidence and is
stamped CONFIDENTIAL. Coverage here is deliberately thorough and adversarial, not just happy-path:

- every allowlisted token accepts a valid value (and the allowlist matches the real stylesheet palette),
- an unknown token name, or any single bad value, rejects the WHOLE mapping (no partial apply),
- colour/font-stack/dimension injection attempts all reject,
- ``render_token_block`` never emits a hostile value even if handed one directly (bypassing the gate),
- the emitted block parses as exactly one ``:root`` rule setting exactly the given tokens,
- a planted positive control that fails loudly if the allowlist is ever opened back up.
"""

from __future__ import annotations

import re

import pytest

from scribble.reporting.render_html import _CSS
from scribble.reporting.tokens import (
    ALLOWED_TOKENS,
    COLOUR_TOKENS,
    DIMENSION_TOKENS,
    FONT_TOKENS,
    render_token_block,
    validate_tokens,
)

_VALID_COLOUR = "#336699"
_VALID_DIMENSION: dict[str, str] = {"radius": "8px", "measure": "72ch", "maxw": "1080px"}
_VALID_FONT_STACK = "Georgia, 'Times New Roman', serif"


# --- the catalogue itself ------------------------------------------------------------------------------

def test_allowlist_groups_partition_cleanly():
    """No token name lives in two kind-groups, and the three groups together ARE the allowlist."""
    colour, dimension, font = set(COLOUR_TOKENS), set(DIMENSION_TOKENS), set(FONT_TOKENS)
    assert colour | dimension | font == ALLOWED_TOKENS
    assert colour.isdisjoint(dimension)
    assert colour.isdisjoint(font)
    assert dimension.isdisjoint(font)


def test_colour_and_dimension_allowlist_matches_the_real_root_palette():
    """Catalogued by READING ``render_html``'s ``_CSS`` (per the ticket), not guessed — this test keeps
    it that way: if the light ``:root {}`` block ever gains/loses/renames a custom property, this fails
    instead of the allowlist silently drifting from the stylesheet it is supposed to describe."""
    root_block = _CSS.split(":root {", 1)[1].split("}", 1)[0]
    declared = set(re.findall(r"--([a-z0-9-]+):", root_block))
    assert declared == set(COLOUR_TOKENS) | set(DIMENSION_TOKENS)


def test_font_tokens_are_not_yet_real_css_custom_properties():
    """Documented, not just assumed: the stylesheet hardcodes font-family inline today, so these three
    tokens name properties that do NOT exist in ``_CSS`` yet — wiring them in is future work."""
    for name in FONT_TOKENS:
        assert f"--{name}:" not in _CSS


# --- happy path: every allowlisted token accepts a valid value -----------------------------------------

@pytest.mark.parametrize("name", COLOUR_TOKENS)
def test_every_colour_token_accepts_a_valid_value(name):
    assert validate_tokens({name: _VALID_COLOUR}) == {name: _VALID_COLOUR}


@pytest.mark.parametrize("name", DIMENSION_TOKENS)
def test_every_dimension_token_accepts_its_valid_value(name):
    value = _VALID_DIMENSION[name]
    assert validate_tokens({name: value}) == {name: value}


@pytest.mark.parametrize("name", FONT_TOKENS)
def test_every_font_token_accepts_a_valid_stack(name):
    assert validate_tokens({name: _VALID_FONT_STACK}) == {name: _VALID_FONT_STACK}


@pytest.mark.parametrize("value", ["#fff", "#FFFFFF", "#a1b2c3", "#a1b2c3d4"])
def test_colour_accepts_all_three_hex_lengths_including_alpha(value):
    assert validate_tokens({"bg": value}) == {"bg": value}


# --- unknown key / partial apply ------------------------------------------------------------------------

def test_unknown_token_name_rejects_the_whole_mapping():
    payload = {"bg": _VALID_COLOUR, "totally-not-a-real-token": "#ffffff"}
    assert validate_tokens(payload) is None


def test_one_bad_value_rejects_the_whole_mapping_not_a_partial_apply():
    """The nine-good-one-bad case named in the ticket: ``accent`` here is a named colour, which the
    strict hex grammar refuses. The whole payload must come back ``None`` -- never eight good keys."""
    payload = {
        "bg": "#111111", "surface": "#222222", "ink": "#333333", "muted": "#444444",
        "line": "#555555", "accent-ink": "#666666", "sev-critical": "#777777",
        "radius": "4px", "accent": "red",
    }
    assert validate_tokens(payload) is None


def test_non_mapping_input_rejects():
    assert validate_tokens([("bg", "#fff")]) is None  # type: ignore[arg-type]
    assert validate_tokens("bg=#fff") is None  # type: ignore[arg-type]


def test_non_string_key_rejects_the_whole_mapping():
    assert validate_tokens({1: "#fff"}) is None  # type: ignore[dict-item]


def test_empty_mapping_is_a_legal_no_op_theme():
    """"Sets nothing" is a valid Theme (falls back to bundled defaults everywhere) -- distinct from an
    invalid payload, which is ``None``."""
    assert validate_tokens({}) == {}
    assert render_token_block({}) == ""


# --- colour injection ------------------------------------------------------------------------------------

@pytest.mark.parametrize(
    "value",
    [
        "red",  # named colour
        "rgb(0, 0, 0)",  # function call
        "hsl(0, 0%, 0%)",  # function call
        "var(--accent)",  # custom-property reference
        "expression(alert(1))",  # legacy IE expression()
        "#fff; }",  # early declaration/rule close
        "#fff;}",
        "#fff}",  # bare closing brace
        "url(x)",
        "#fff\nbody{display:none}",  # embedded newline
        "#" + "f" * 200,  # way over length
        "",  # empty
        "#gggggg",  # not hex digits
        "#12345",  # illegal length (5 hex digits)
        "#1234567",  # illegal length (7 hex digits)
        "fff",  # missing leading '#'
        123456,  # not a string at all
        None,
    ],
)
def test_colour_injection_attempts_all_reject(value):
    assert validate_tokens({"bg": value}) is None


# --- font-stack injection ----------------------------------------------------------------------------

@pytest.mark.parametrize(
    "value",
    [
        "Arial, sans-serif; }",  # semicolon + brace
        "url(evil.css)",  # function call
        "@import url(evil.css)",  # at-rule
        "Arial\\, sans-serif",  # backslash / CSS escape
        "Arial\nsans-serif",  # newline
        "Arial{display:none}",  # brace
        "javascript:alert(1)",  # colon not in the charset either way
        ",",  # nothing but an empty slot
        "Arial,,sans-serif",  # doubled comma -> empty slot
        ",Arial",  # leading comma -> empty slot
        "Arial,",  # trailing comma -> empty slot
        "",
        "x" * 300,  # way over length
        99,  # not a string
    ],
)
def test_font_stack_injection_attempts_all_reject(value):
    assert validate_tokens({"font-body": value}) is None


# --- dimension injection / out-of-bounds ---------------------------------------------------------------

@pytest.mark.parametrize(
    "value",
    [
        "-10px",  # negative
        "10vh",  # unit not in the allowlist
        "8px;",  # trailing semicolon
        "8px}",  # trailing brace
        "999999px",  # exceeds the px ceiling
        "5000%",  # exceeds the percent ceiling
        "10 px",  # whitespace between number and unit
        "px",  # no number
        "10",  # no unit
        "",
        "1e10px",  # scientific notation
        "calc(100% - 8px)",  # function call
        123,  # not a string
    ],
)
def test_dimension_injection_and_out_of_bound_attempts_all_reject(value):
    assert validate_tokens({"radius": value}) is None


# --- render_token_block: emission -----------------------------------------------------------------------

_VAR_RE = re.compile(r"--([a-z0-9-]+):\s*([^;]+);")


def test_render_token_block_emits_one_root_rule_with_exactly_the_given_tokens():
    tokens = validate_tokens(
        {"bg": "#112233", "radius": "6px", "font-mono": "Menlo, monospace"}
    )
    assert tokens is not None
    css = render_token_block(tokens)
    assert css.count(":root") == 1
    stripped = css.strip()
    assert stripped.startswith(":root {")
    assert stripped.endswith("}")
    found = dict(_VAR_RE.findall(css))
    assert found == tokens


def test_render_token_block_never_emits_a_hostile_value_that_slipped_past_the_gate():
    """The gate (``validate_tokens``) is what a real caller uses -- prove it catches this payload --
    but also prove the emitter refuses it directly, in case a future caller ever skips the gate."""
    hostile_raw = {"bg": "#fff}<style>body{display:none}</style>"}
    assert validate_tokens(hostile_raw) is None
    with pytest.raises(ValueError):
        render_token_block(hostile_raw)


def test_render_token_block_raises_rather_than_partially_emit_a_mixed_dict():
    """One good token plus one hand-crafted bad one, handed straight to the emitter (bypassing
    ``validate_tokens``): it must refuse the whole call, not emit the good half."""
    mixed = {"bg": "#112233", "accent": "red; } .confidential { display: none"}
    with pytest.raises(ValueError):
        render_token_block(mixed)


def test_render_token_block_of_an_unknown_name_raises():
    with pytest.raises(ValueError):
        render_token_block({"not-a-real-token": "#112233"})


# --- planted positive control -------------------------------------------------------------------------

def test_planted_control_allowlist_stays_closed_against_dangerous_names_and_values():
    """Positive control for the whole module. If a future change ever "simplified" this by widening the
    allowlist to accept raw CSS property names, or loosened the colour grammar to accept anything
    starting with '#' / containing a hex run, THIS test starts failing instead of the regression going
    unnoticed. ``background:url()`` beaconing the report open the moment it's opened is exactly the
    exfiltration primitive named in the module docstring."""
    dangerous_names = ("background", "background-image", "content", "cursor", "list-style-image", "src")
    for name in dangerous_names:
        assert name not in ALLOWED_TOKENS

    # Even under a real, allowlisted colour token, a url()-bearing value must never survive -- this is
    # the exact case a "starts with '#'" or "contains hex digits" regression would wrongly accept.
    assert validate_tokens({"bg": "#fff url(https://evil.example/beacon.png)"}) is None
    assert validate_tokens({"accent-wash": "url(https://evil.example/beacon.png)"}) is None
    assert validate_tokens({"bg": "#fff; } body { background: url(https://evil.example/b)"}) is None


# --- unbalanced quotes in a font stack (regression: #101 adversarial review) --------------------------

@pytest.mark.parametrize(
    "value",
    [
        "'",                              # a lone quote as the entire value
        '"',
        "Georgia, 'Times New Roman",      # opened, never closed -- the reported cross-token case
        "'Times New Roman, Georgia",
        "Georgia', Helvetica",            # closes a string that was never opened
        "'Times New Roman\", Georgia",    # mismatched pair
        "'a'b'",                          # re-opens after closing
        'He"lvetica',                     # bare family carrying a quote
        "'Times New Roman'x",             # trailing junk after the closing quote
    ],
)
def test_unbalanced_quotes_in_a_font_stack_are_rejected(value):
    """An unterminated CSS string does not stay inside its own declaration: the declaration-list
    grammar consumes tokens into the open string until the next unescaped ';' -- which is the one
    meant to end the NEXT declaration. So a stray quote silently swallows a sibling token whole."""
    for token in FONT_TOKENS:
        assert validate_tokens({token: value}) is None, f"{token}={value!r} should have been refused"


def test_balanced_quotes_in_a_font_stack_still_pass():
    """The fix must not break real font stacks -- quoted multi-word families are the whole reason the
    charset admits quotes at all."""
    good = "'Times New Roman', Georgia, \"IBM Plex Mono\", serif"
    for token in FONT_TOKENS:
        assert validate_tokens({token: good}) == {token: good}


def test_a_sibling_declaration_cannot_be_swallowed():
    """The end-to-end property the ticket requires: the emitted block sets EXACTLY the tokens given.

    Before the fix, validate_tokens accepted this payload wholesale and render_token_block emitted an
    unterminated string in --font-body that absorbed the entire --font-mono declaration, so a caller
    asking for three tokens silently got two.
    """
    hostile = {
        "font-body": "Georgia, 'Times New Roman",
        "font-mono": "Consolas",
        "bg": "#112233",
    }
    assert validate_tokens(hostile) is None

    # ...and the honest version of the same payload really does emit all three declarations.
    honest = dict(hostile, **{"font-body": "Georgia, 'Times New Roman'"})
    validated = validate_tokens(honest)
    assert validated is not None
    block = render_token_block(validated)
    for name in ("--font-body", "--font-mono", "--bg"):
        assert f"{name}:" in block
    # every declaration terminated: one ';' per token, and quotes balanced across the whole block
    assert block.count(";") == len(honest)
    assert block.count("'") % 2 == 0
    assert block.count('"') % 2 == 0
