"""Composing a Theme into the CSS the report actually carries — the integration seam.

The pieces this joins were each built in isolation: :mod:`theme_files` loads a bundled Theme,
:mod:`tokens` says which values are legal and emits a declaration block.
What none of them can decide alone is **where in the cascade the result has to sit**, because that
depends on the base stylesheet — and in this stylesheet, specificity is load-bearing.

## Why a plain ``:root { }`` override is not enough

``render_html._CSS`` carries FOUR palettes, and two of them outrank a naive override:

- ``:root`` — the light palette. 0-1-0.
- ``:root:not([data-theme="light"])`` inside ``prefers-color-scheme: dark``. **0-2-0.**
- ``:root[data-theme="dark"]``. **0-2-0.**
- ``@media print`` → ``:root:not([data-theme="dark"]), :root[data-theme="dark"]``. **0-2-0.**

Appending a Theme's tokens as ``:root { }`` (0-1-0) therefore loses to the dark palette on a
dark-stamped report, and loses to the print rule on *every* report. A branded deliverable would print
entirely unbranded, and nothing on screen would reveal it. That is not hypothetical: the comment above
the print rule records a shipped bug where a dark-mode browser printed ``#7ee0bc`` client names onto
white paper at 1.6:1. So the override is emitted at ``:root:root`` — also 0-2-0, but later in the
sheet, which wins the tie.

## Why paper is OPT-IN, and why that is not timidity

The print rule is not an obstacle to route around; it is a deliberate control. It forces
paper-appropriate values for *every* colour token, so a dark-stamped report still prints legibly.

A Theme's ordinary ``[tokens]`` are SCREEN values. The bundled dark Theme's ``--sev-high`` is
``#ef8a44``, tuned against a near-black panel — on white paper it is a washed-out orange where the
paper ramp uses ``#c2410c``. An earlier cut of this module carried "brand identity" tokens (accent,
severity, type) to paper automatically on the theory that a firm's accent is chosen against white.
That theory is right for a *brand* Theme and wrong for every screen Theme, and
``test_report_print_media.py::test_a_dark_template_still_prints_on_paper_colours`` caught it
immediately: the dark orange reached the page.

So paper theming is explicit. A Theme reaches paper only by declaring ``[print_tokens]``, having
actually reasoned about contrast on white — which is exactly the work a brand's paper palette
requires. Absent that table, the base sheet keeps full control of paper and the shipped guarantee is
untouched. Screen still gets everything.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from scribble.reporting import theme_files
from scribble.reporting.marks import ResolvedMark, resolve_mark
from scribble.reporting.theme_registry import ResolvedTheme
from scribble.reporting.tokens import render_token_block, validate_tokens

# 0-2-0, matching the dark and print rules rather than trying to outrank them; later in the sheet, so
# it wins the tie. Deliberately NOT `!important`: that would leak a screen palette onto paper for a
# Theme that never declared `[print_tokens]`, which is the exact failure this module is built around.
_OVERRIDE_SELECTOR = ":root:root"

_log = logging.getLogger(__name__)


def _assert_style_safe(css: str) -> str:
    """Defense in depth at the ONE point the CSS becomes part of an HTML document.

    Nothing that reaches here can currently carry ``</style>``: every value has been through
    ``tokens``' closed grammar, and ``theme_files`` holds a font ``family`` to ``[A-Za-z0-9 -]+``. But
    the checks that make that true live three modules away, and the Theme payload is explicitly slated
    to start arriving from an operator (``override`` provenance, Tier B). A breakout here would put
    attacker-chosen markup inside a document that also embeds client evidence, so the guarantee is
    asserted where it is *relied upon* rather than only where it is established.
    """
    if "</style" in css.lower():
        _log.error("scribble: report Theme CSS contained a style-closing sequence; refusing it")
        return ""
    return css


@dataclass(frozen=True)
class ThemeAssets:
    """Everything a Theme contributes to one rendered report."""

    css: str
    mark: ResolvedMark | None = None

    @property
    def is_empty(self) -> bool:
        return not self.css and self.mark is None


def build_theme_assets(resolved: ResolvedTheme) -> ThemeAssets:
    """Compose an already-resolved Theme into the CSS and Mark the renderer splices in.

    This is a pure composer: ``theme_registry.resolve_theme`` has already found the Theme across all
    three Provenances and parsed it. Loading used to happen here, which only ever worked for a BUNDLED
    Theme — the name was looked up as a filename in Scribble's own package, so an installed or
    operator-supplied Theme could never resolve no matter that the rest of the machinery could see it.

    Returns empty assets — never raises — when the Theme carries no payload (``auto`` has none by
    design: it *is* the base stylesheet) or when its payload fails re-validation. A Theme that cannot
    be composed must degrade to the shipped appearance, not to a broken page.
    """
    loaded = resolved.file
    if loaded is None:
        # Not an error: `auto` has no payload, and an unresolvable name already degraded to `auto` in
        # the registry (which logs it there). Nothing to say.
        return ThemeAssets(css="")
    # theme_files validates on load, but re-validate at the render boundary anyway: this is the same
    # belt-and-braces rule cream learned the hard way, where the write path and the render path
    # disagreed about what was acceptable and the renderer was the lenient one.
    tokens = validate_tokens(loaded.tokens)
    if tokens is None:
        # Reaching here means the two validation passes DISAGREED — load accepted a payload the render
        # boundary rejects. That is a bug in the grammar, not bad input, and it must never be silent.
        _log.error(
            "scribble: report Theme %r passed load-time validation but failed at the render "
            "boundary; rendering unthemed. This is a grammar inconsistency, not bad data.",
            resolved.theme.name,
        )
        return ThemeAssets(css="")

    parts: list[str] = []

    # Composition gets the same "degrade, never 500" contract as loading. Only `load_theme_file` used to
    # be guarded, so a font read or an emission failure raised straight through `_render_document` and
    # turned a cosmetic Theme fault into a 500 on `/engagements/<id>/report` — the report route failing
    # closed over branding, which is precisely the trade this module exists to refuse.
    try:
        # Provenance is threaded in, not assumed: an override Theme must not have its declared
        # `[fonts].package` honoured, because resolving a package name imports it.
        font_css = theme_files.build_font_face_css(loaded, provenance=resolved.provenance)
        if font_css:
            parts.append(font_css if font_css.endswith("\n") else font_css + "\n")

        if tokens:
            parts.append(f"@media screen {{\n{render_token_block(tokens, _OVERRIDE_SELECTOR)}}}\n")

        # Paper ONLY if the Theme declared paper values. See the module docstring: screen tokens are
        # tuned against a screen, and shipping them to paper is how the dark ramp reached a printed
        # deliverable.
        paper = validate_tokens(getattr(loaded, "print_tokens", {}) or {})
        if paper:
            parts.append(f"@media print {{\n{render_token_block(paper, _OVERRIDE_SELECTOR)}}}\n")
    except Exception as exc:  # noqa: BLE001 — degrade to the base sheet, never to a 500
        _log.warning(
            "scribble: report Theme %r failed to compose, rendering unthemed: %s",
            resolved.theme.name,
            exc,
        )
        return ThemeAssets(css="")

    # The Mark, gated by PROVENANCE. `resolve_mark` is the single decision point both the write path
    # and the render path call, which is the whole fix for the cream bug where an API and a renderer
    # independently decided what a safe logo was and drifted apart. Re-checking here rather than
    # trusting that a stored/parsed Mark is still safe to splice is cream's belt-and-braces instinct
    # kept, just pointed at one function instead of two: a bundled or installed Theme may carry SVG
    # (installing a package is already arbitrary code execution), an operator-uploaded `override`
    # Theme is raster-only and never even reaches the XML parser.
    mark = None
    if loaded.logo_svg:
        mark = resolve_mark(loaded.logo_svg, provenance=resolved.provenance)
        if mark is None:
            # Loud, for the same reason a failed Theme load is loud: a silently absent logo is a
            # client deliverable that went out looking wrong with nothing to explain why.
            _log.warning(
                "scribble: report Theme %r declared a Mark that was refused at %r provenance; "
                "rendering without it",
                resolved.theme.name,
                resolved.provenance,
            )

    return ThemeAssets(css=_assert_style_safe("".join(parts)), mark=mark)
