"""Composing a Theme into the CSS the report actually carries — the integration seam.

The pieces this joins were each built in isolation: :mod:`theme_files` loads a bundled Theme,
:mod:`tokens` says which values are legal and emits a declaration block, :mod:`marks` vets the logo.
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

from dataclasses import dataclass

from scribble.reporting import theme_files
from scribble.reporting.marks import ResolvedMark
from scribble.reporting.themes import ReportTheme
from scribble.reporting.tokens import validate_tokens

# 0-2-0, matching the dark and print rules rather than trying to outrank them; later in the sheet, so
# it wins the tie. Deliberately NOT `!important`: that would leak a screen palette onto paper for a
# Theme that never declared `[print_tokens]`, which is the exact failure this module is built around.
_OVERRIDE_SELECTOR = ":root:root"


@dataclass(frozen=True)
class ThemeAssets:
    """Everything a Theme contributes to one rendered report."""

    css: str
    mark: ResolvedMark | None

    @property
    def is_empty(self) -> bool:
        return not self.css and self.mark is None


def _declaration_block(selector: str, tokens: dict[str, str]) -> str:
    body = "".join(f"  --{name}: {value};\n" for name, value in sorted(tokens.items()))
    return f"{selector} {{\n{body}}}\n"


def build_theme_assets(theme: ReportTheme) -> ThemeAssets:
    """Resolve ``theme`` to the CSS (and Mark) the renderer should splice in after the base sheet.

    Returns empty assets — never raises — when the Theme has no bundled file (``auto`` has none by
    design: it *is* the base stylesheet's own behaviour) or when its payload fails validation. A Theme
    that cannot be resolved must degrade to the shipped appearance, not to a broken page.
    """
    loaded = None
    try:
        loaded = theme_files.load_theme_file(theme.name)
    except Exception:  # noqa: BLE001 — a malformed Theme degrades to the base sheet, never to a 500
        loaded = None
    if loaded is None:
        return ThemeAssets(css="", mark=None)

    # theme_files validates on load, but re-validate at the render boundary anyway: this is the same
    # belt-and-braces rule cream learned the hard way, where the write path and the render path
    # disagreed about what was acceptable and the renderer was the lenient one.
    tokens = validate_tokens(loaded.tokens)
    if tokens is None:
        return ThemeAssets(css="", mark=None)

    parts: list[str] = []

    font_css = theme_files.build_font_face_css(loaded)
    if font_css:
        parts.append(font_css if font_css.endswith("\n") else font_css + "\n")

    if tokens:
        parts.append(f"@media screen {{\n{_declaration_block(_OVERRIDE_SELECTOR, tokens)}}}\n")

    # Paper ONLY if the Theme declared paper values. See the module docstring: screen tokens are tuned
    # against a screen, and shipping them to paper is how the dark ramp reached a printed deliverable.
    paper = validate_tokens(getattr(loaded, "print_tokens", {}) or {})
    if paper:
        parts.append(f"@media print {{\n{_declaration_block(_OVERRIDE_SELECTOR, paper)}}}\n")

    # Marks are NOT wired yet, and this is the honest state rather than an oversight: `reporting.marks`
    # (#104) is built and tested, but the Theme *schema* carries only a reserved `[marks]` placeholder —
    # `ThemeFile` has no field to read a logo out of, so there is nothing to resolve. The seam is here,
    # one line, so that whoever adds the schema field wires it in ONE place:
    #
    #     mark = resolve_mark(loaded.mark, provenance="bundled")
    #
    # and gets the provenance gate for free. "bundled" is what permits SVG at all; an operator-supplied
    # override Theme arrives at "override" and is raster-only. That decision lives in marks.resolve_mark,
    # never here, so the write path and the render path cannot drift apart — which is exactly how cream
    # shipped a real bug (its API refused SVG while its renderer accepted it).
    return ThemeAssets(css="".join(parts), mark=None)
