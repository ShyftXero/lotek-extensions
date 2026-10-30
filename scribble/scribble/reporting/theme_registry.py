"""Resolving a Theme NAME to a Theme, across all three Provenances — the thing that was missing.

``themes.py`` holds a frozen tuple of three: ``auto``, ``light``, ``dark``. That is why an installed
Theme was invisible even once discovery existed — ``get_theme("synoptek")`` looked in a hardcoded dict,
missed, and fell back to ``auto``. Commit ``75159ed`` cut discovery for exactly that reason ("entry-point
Theme discovery that ``get_theme`` never consults"). This module is the consulting.

## Why it is a separate module

``themes`` must not import ``theme_files``: ``theme_files`` imports ``themes`` (for the stamp
vocabulary), so the reverse direction is an import cycle. Keeping the merge here leaves ``themes`` a
leaf — a pure vocabulary module — and gives the merge somewhere to live that may freely import both.

## Override Themes, and why a callback rather than a query

An ``override`` Theme is a DB row. Nothing under ``reporting/`` touches a session — the renderers are
pure functions over a frozen ``ReportContext``, which is what lets the whole report suite run without a
database. So the override lookup arrives as an injected callable, exactly like ``artifact_bytes`` and
``artifact_url`` already do for evidence. A caller with no database (a test, a standalone render) simply
omits it and gets bundled + installed.

## Precedence, and why bundled wins

**bundled > installed > override.** Discovery already excludes an installed Theme whose name collides
with a bundled one, and ``themes_api`` refuses an override upload that collides with either — so in a
healthy install these sets are disjoint and precedence never arbitrates. It is stated and implemented
anyway, because "the sets are disjoint" is a property maintained by two other modules, and a registry
that quietly resolved differently if either ever slipped would re-skin a client deliverable without
saying so. The ordering runs least-trusted-last: an operator pasting data into a form cannot displace a
Theme that ships in the wheel.

## Trust travels with the Theme

Every resolution carries its :class:`ResolvedTheme.provenance`, and that is not decoration —
``marks.resolve_mark`` keys the SVG gate off it (bundled/installed may carry SVG; override is
raster-only, because installing a package is already arbitrary code execution while pasting into a form
is not). Callers read that one field instead of re-deriving trust from *how* they got the object, which
is the kind of ambient reasoning that gets inverted by a later refactor.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass

from scribble.reporting import theme_files
from scribble.reporting.theme_discovery import discover_installed_themes
from scribble.reporting.theme_files import FontFace, ThemeFile, ThemeFileError
from scribble.reporting.themes import DEFAULT_THEME, THEMES, ReportTheme

PROVENANCE_BUNDLED = "bundled"
PROVENANCE_INSTALLED = "installed"
PROVENANCE_OVERRIDE = "override"

# ``name -> TOML text``, or ``None`` if that name is not an override Theme. Supplied by a caller that
# has a database; omitted by one that does not.
OverrideLookup = Callable[[str], str | None]

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ResolvedTheme:
    """A Theme name resolved to its payload, plus where it came from."""

    theme: ReportTheme
    file: ThemeFile | None
    provenance: str

    @property
    def carries_payload(self) -> bool:
        return self.file is not None


def _report_theme_from_file(file: ThemeFile) -> ReportTheme:
    """The switcher/stamp view of a Theme that arrived as a file rather than a registry entry.

    ``stamp`` comes from the Theme's own ``[identity].stamp``, which is why that field exists: a
    bundled Theme has a registry entry supplying it, an installed or override Theme has none, and
    without it every non-bundled Theme would stamp nothing and inherit the viewer's dark palette
    underneath its own light-tuned colours.
    """
    return ReportTheme(name=file.name, label=file.label, stamp=file.stamp)


def resolve_theme(
    name: str | None, *, override_lookup: OverrideLookup | None = None
) -> ResolvedTheme:
    """Resolve ``name`` across bundled, installed and override Themes.

    Never raises, and never returns ``None``: an unknown or malformed Theme degrades to the base
    stylesheet's own appearance (``auto``, no payload), which is the shipped look. ``?theme=`` is an
    untrusted query value and a client deliverable must render regardless.
    """
    wanted = (name or "").strip().lower()

    # 1. Bundled. A bundled name may or may not have a payload file: `auto` deliberately has none — it
    #    IS the base stylesheet — while `light`/`dark` do.
    if wanted in THEMES:
        file = None
        try:
            file = theme_files.load_theme_file(wanted)
        except ThemeFileError as exc:
            # A broken BUNDLED file is a build defect, not bad input -- it shipped in the wheel. The
            # registry entry still resolves so the report renders; the log is how anyone finds out.
            _log.error("scribble: bundled report Theme %r is malformed, rendering unthemed: %s",
                       wanted, exc)
            file = None
        return ResolvedTheme(theme=THEMES[wanted], file=file, provenance=PROVENANCE_BUNDLED)

    if not wanted:
        return ResolvedTheme(theme=THEMES[DEFAULT_THEME], file=None, provenance=PROVENANCE_BUNDLED)

    # 2. Installed. Materialise the TOML only now, for the one Theme actually selected — discovery
    #    deliberately does not call the loaders, so a firm-brand package's file read does not happen on
    #    every render of every OTHER report.
    descriptor = discover_installed_themes().themes.get(wanted)
    if descriptor is not None:
        file = _parse_or_none(wanted, descriptor.load_toml, PROVENANCE_INSTALLED)
        if file is not None:
            return ResolvedTheme(
                theme=_report_theme_from_file(file), file=file, provenance=PROVENANCE_INSTALLED
            )

    # 3. Override.
    if override_lookup is not None:
        file = _parse_or_none(wanted, lambda: override_lookup(wanted), PROVENANCE_OVERRIDE)
        if file is not None:
            return ResolvedTheme(
                theme=_report_theme_from_file(file), file=file, provenance=PROVENANCE_OVERRIDE
            )

    return ResolvedTheme(theme=THEMES[DEFAULT_THEME], file=None, provenance=PROVENANCE_BUNDLED)


def _parse_or_none(name: str, load: Callable[[], str | None], provenance: str) -> ThemeFile | None:
    """Materialise and parse one Theme's TOML, or ``None`` — degrading LOUDLY.

    Broad by intent. ``load`` here is third-party code (an installed package's entry point) or operator
    data, and every failure mode — an import-time error, a non-``str`` return, malformed TOML, a token
    outside the closed allowlist — means the same thing to a render: this Theme is unusable, and the
    report must still go out.

    Every one of those paths logs. Degrading safely and degrading silently are different decisions, and
    only the first is wanted: a Theme that fails here still renders a perfectly clean report, just an
    UNBRANDED one, so without a log the single artifact this feature exists to produce reaches a client
    looking wrong with nothing anywhere to say why. ``CLAUDE.md`` records how baffling lotek's
    swallow-everything extension discovery made a real bug, and INV-EXT-05 requires a denial to be loud.
    """
    try:
        text = load()
    except Exception as exc:  # noqa: BLE001
        _log.warning(
            "scribble: %s report Theme %r failed to load its TOML, ignoring it: %s",
            provenance, name, exc,
        )
        return None
    if not isinstance(text, str) or not text:
        _log.warning(
            "scribble: %s report Theme %r returned %s instead of TOML text, ignoring it",
            provenance, name, type(text).__name__,
        )
        return None
    try:
        return theme_files._parse_theme_toml(name, text)
    except Exception as exc:  # noqa: BLE001
        _log.warning(
            "scribble: %s report Theme %r failed schema validation, ignoring it: %s",
            provenance, name, exc,
        )
        return None


# ── Delivery Snapshot (ext#106): freeze a resolved Theme to JSON and back ─────────────────────────

#: Bumped only on a breaking change to the snapshot shape. ``thaw_resolved_theme`` reads defensively
#: (``.get`` with fallbacks) so a v1 row keeps thawing after a field is added.
_SNAPSHOT_VERSION = 1


def freeze_resolved_theme(resolved: ResolvedTheme) -> dict:
    """Serialize a :class:`ResolvedTheme` to a self-contained, JSON-safe snapshot (ext#106).

    Captures the Theme's whole rendered payload -- Tokens, print Tokens, Mark (``logo_svg``), stamp and
    font faces -- plus its Provenance, so :func:`thaw_resolved_theme` can reproduce the exact same
    render WITHOUT resolving any Theme by name. That independence is the point: the install default can
    change, an override row can be edited, the source Theme can be uninstalled, and a report delivered
    off this snapshot still renders byte-identically.

    NOT frozen: font BYTES. The faces are captured by filename + ``font_package``, so an installed
    Theme's custom typeface re-embeds only while that package stays installed; after it is uninstalled
    the faces fall back to the stack (``build_font_face_css`` degrades to ``""`` for an absent file --
    by design). The bundled Themes this ships with carry no faces, so the DoD (re-render identically
    after the install default changes) is unaffected. ponytail: capture face bytes too if a delivered
    report must keep its exact typeface across an uninstall.
    """
    file = resolved.file
    return {
        "v": _SNAPSHOT_VERSION,
        "provenance": resolved.provenance,
        "theme": {
            "name": resolved.theme.name,
            "label": resolved.theme.label,
            "stamp": resolved.theme.stamp,
        },
        "file": None if file is None else {
            "name": file.name,
            "label": file.label,
            "tokens": dict(file.tokens),
            "embed_fonts": file.embed_fonts,
            "faces": [
                {"family": f.family, "weight": f.weight, "style": f.style, "file": f.file}
                for f in file.faces
            ],
            "stamp": file.stamp,
            "print_tokens": dict(file.print_tokens),
            "font_package": file.font_package,
            "logo_svg": file.logo_svg,
        },
    }


def thaw_resolved_theme(snapshot: dict) -> ResolvedTheme:
    """Reconstruct a :class:`ResolvedTheme` from a :func:`freeze_resolved_theme` snapshot.

    Resolves NOTHING by name -- it rebuilds the payload straight from the stored dict -- which is what
    makes a delivered report render from its own frozen branding even after the Theme it came from is
    gone. ``build_theme_assets`` still re-validates the thawed Tokens through the closed grammar and
    re-vets the Mark through ``marks.resolve_mark`` (keyed on the stored Provenance) at the render
    boundary, so a tampered row degrades to the base sheet rather than injecting -- the same
    belt-and-braces re-check a freshly resolved Theme already gets there.

    Reads defensively: a missing field falls back to the ``auto``/empty default rather than raising, so
    a partially-written or future-shaped row never 500s a client deliverable.
    """
    t = snapshot.get("theme") or {}
    theme = ReportTheme(
        name=t.get("name", DEFAULT_THEME), label=t.get("label", ""), stamp=t.get("stamp", "")
    )
    f = snapshot.get("file")
    file = None
    if isinstance(f, dict):
        file = ThemeFile(
            name=f.get("name", theme.name),
            label=f.get("label", theme.label),
            tokens=dict(f.get("tokens") or {}),
            embed_fonts=bool(f.get("embed_fonts", False)),
            faces=tuple(
                FontFace(family=x["family"], weight=x["weight"], style=x["style"], file=x["file"])
                for x in (f.get("faces") or ())
            ),
            stamp=f.get("stamp", ""),
            print_tokens=dict(f.get("print_tokens") or {}),
            logo_svg=f.get("logo_svg"),
            **({"font_package": f["font_package"]} if f.get("font_package") else {}),
        )
    return ResolvedTheme(
        theme=theme, file=file, provenance=snapshot.get("provenance", PROVENANCE_BUNDLED)
    )


def list_all_themes(*, override_names: tuple[str, ...] = ()) -> list[ReportTheme]:
    """Every selectable Theme, in switcher order: bundled, then installed, then override.

    Cheap by design — it must be, since the report toolbar renders it on every page view. Bundled
    entries come from the frozen registry and installed ones from cached discovery metadata; NO Theme's
    TOML is parsed here, so a Theme broken enough to fail parsing still appears in the switcher and
    degrades visibly when selected, rather than vanishing from the list with no explanation.
    """
    out: list[ReportTheme] = list(THEMES.values())
    seen = {t.name for t in out}

    for name in sorted(discover_installed_themes().themes):
        if name not in seen:
            # Labelled from the name until its TOML is parsed. Parsing every installed Theme just to
            # read a display label would put third-party file reads on every page view.
            out.append(ReportTheme(name=name, label=name.replace("-", " ").title(), stamp=""))
            seen.add(name)

    for name in override_names:
        folded = (name or "").strip().lower()
        if folded and folded not in seen:
            out.append(ReportTheme(name=folded, label=folded.replace("-", " ").title(), stamp=""))
            seen.add(folded)

    return out
