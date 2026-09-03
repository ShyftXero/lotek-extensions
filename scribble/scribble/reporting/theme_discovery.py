"""Discovery of **installed** report Themes — the ones that do NOT ship inside Scribble.

``scribble/CONTEXT.md`` names three Provenances a Theme can carry: **bundled** (ships inside Scribble),
**installed** (a separate package Scribble discovers), and **override** (data an operator supplied at
runtime). This module produces the middle one. A firm that wants its own palette/type/marks on every
report it issues ships a small pip-installable package advertising a
``[project.entry-points."scribble.report_themes"]`` entry — the same idiom this very package uses one
level up to advertise itself to lotek (``[project.entry-points."lotek.extensions"]`` in
``pyproject.toml``) — and Scribble finds it here without a code change or a redeploy of Scribble itself.

**Provenance is not cosmetic.** #104's Mark gate keys off it directly: bundled and installed Themes may
carry a raw SVG Mark, override is raster-only, because *installing a package is already arbitrary code
execution* — a Theme that arrived that way is trusted the way the rest of Scribble's own code is
trusted, while an *override* (data an operator pasted into a form at runtime) never ran as code to get
here and so earns no such trust regardless of how convincing its Marks look. Every
:class:`InstalledThemeDescriptor` this module returns carries ``provenance == PROVENANCE_INSTALLED`` as
a real field for exactly this reason — so a caller deciding "may this Theme's Marks skip the sanitiser"
reads one attribute instead of re-deriving trust from *how it got the object*, which is the kind of
ambient reasoning that is easy to get backwards under a later refactor.

## Payload contract: TOML text, not an opaque object

The one thing that changed from an earlier cut of this module (see git history around #103/#75159ed):
an entry point used to resolve to an unspecified "loader" callable, with nothing pinning what it
returned — which is part of why that cut had no caller at all. This module now pins it: **an entry
point resolves to a zero-argument callable returning the Theme's TOML TEXT**, i.e. a ``str`` with
exactly the same four-section grammar (``[identity]``/``[tokens]``/``[fonts]``/``[marks]``) documented
in ``theme_files``'s module docstring for a *bundled* Theme file.

That is a deliberate reuse, not a coincidence: ``theme_files._parse_theme_toml`` is already the pure,
fully-validating parser for the bundled case — closed Token allowlist, font-face grammar, the whole
schema. Returning text means an installed Theme's payload goes through the IDENTICAL parser and the
IDENTICAL closed-allowlist validation as a bundled one before anything reaches a page. There is no
second schema for an installed Theme to satisfy and no second validator to drift out of sync with the
first — an installed Theme's *code* got to run (importing it is how ``ep.load()`` found the callable at
all), but its *content* gets exactly the same scrutiny as a file Scribble ships itself. Concretely, a
render-time caller is expected to do::

    text = descriptor.load_toml()
    theme_file = theme_files._parse_theme_toml(descriptor.name, text)

``load_toml`` is never called by this module — see "Why the callable is never invoked here" below — but
it IS wrapped so that calling it late, at render time, fails with a clear, on-the-spot ``TypeError``
if the installed package's callable returns something other than a ``str`` (a dict, ``None``, bytes,
...). Without that wrapper, a misbehaving entry point's non-string return value would instead flow
straight into ``tomllib.loads`` several calls away from where the actual mistake was made, surfacing a
confusing internal error nobody could trace back to "this installed package's entry point is wrong". An
error must not be a crash: raising loudly, immediately, with the entry point's name attached, is the
difference.

## Why the callable is never invoked here

An entry point's target is not assumed to be any particular payload beyond "callable, and later found
to return `str`": ``ep.load()`` (import + attribute lookup) happens during discovery, because that is
the only way to hand the caller "a callable" at all — but the callable itself is never INVOKED here.
Materialising the actual TOML text is left to whoever is about to render with this Theme selected. Two
reasons to split it there rather than call it eagerly:

1. Discovery runs on every request that needs the Theme switcher's option list (see
   ``themes.list_themes`` for the bundled equivalent); a firm-brand package's ``load_toml`` — reading a
   file off disk, or worse, over the network — has no business running on every page view of every
   OTHER report.
2. It keeps this module's own error handling honest: a raise from ``ep.load()`` means "this entry point
   is not resolvable" (a discovery-shaped failure, collected below); a raise (or bad return type) from
   actually materialising the text is a render-shaped failure and belongs to the caller that triggered
   it, with the context (which report, which request) that only that caller has.

## Name collision policy: bundled wins, and the collision is surfaced, never silent

An installed Theme whose name matches a bundled one is EXCLUDED from the returned mapping outright —
not merely shadowed-if-you-merge-in-the-wrong-order — and recorded in ``ThemeDiscovery.collisions``
instead. Argued: a report is a client deliverable, and the failure mode of "a transitively installed
dependency happened to publish a Theme named `default` and a report silently re-skinned" is worse than
the failure mode of "the firm's installed Theme was refused and an operator has to rename it" — the
first is invisible until a client notices unexpected branding on a delivered PDF; the second is a loud,
fixable error at discover-then-render time. Baking the exclusion into discovery itself (rather than
leaving "bundled wins" as a rule the caller must apply correctly at every merge site) means a future
caller cannot get this backwards by merging the two dicts in the wrong order — the colliding name is
simply never IN the returned mapping to begin with. Two installed packages independently claiming the
same name are treated the same way (first one ``entry_points()`` yields wins, the rest collide) for the
identical reason, though note ``entry_points()`` order across distributions is not a documented stable
contract, so which one of two colliding INSTALLED packages wins is best-effort, not a guarantee — the
bundled-vs-installed case is the one that actually matters, because Scribble controls that ordering
absolutely.

Names are matched CASE-FOLDED throughout, because ``themes.get_theme`` lower-cases before looking up.
Without folding here, an installed Theme called ``"Dark"`` would not register as colliding with the
bundled ``dark`` — and would then be selected by ``?theme=dark`` anyway, which is precisely the
surprise re-skin the "bundled always wins" policy exists to prevent.

## Error surfacing

``CLAUDE.md`` records a real incident: lotek's own extension discovery swallows every exception by
design, which once made a genuine mounting bug ("a PAT write 403s") baffling to debug because nothing
said an extension had failed to load at all. This module does the opposite on purpose — a broken
installed Theme (an import error in the package, a malformed entry point, a target that is not
callable) is never simply absent from the result. It is collected into ``ThemeDiscovery.errors`` so a
caller can log it or render it on an admin page, while every OTHER entry point is still resolved.
Discovery itself never raises: a corrupted `importlib.metadata` cache degrades to an empty result, not
a crashed page.

## Caching

Entry points do not change at runtime — the set of installed distributions is fixed for the life of
the process — so the result of a discovery pass against the REAL environment (``entry_points=None`` and
``bundled_names=None``, i.e. the shape every non-test caller actually uses) is memoised in a simple
module-level variable after the first call, so a page that needs the Theme switcher's option list on
every request does not re-touch ``importlib.metadata`` (and re-import every installed Theme package)
on every single one. A call that supplies either seam explicitly (as every test in this module's own
suite does) always runs fresh and is never cached or cache-populating, precisely so tests can exercise
different synthetic environments back-to-back without interference. :func:`clear_theme_discovery_cache`
resets the memoised value; call it in a test that hits the real default path more than once (real
``importlib.metadata`` monkeypatched via ``entry_points=None``) so one case's patched environment cannot
leak into the next.

This module is pure: no Flask, no logging configuration, no I/O beyond `importlib.metadata`. It returns
data and lets the caller decide what to do with it.
"""

from __future__ import annotations

import importlib.metadata
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from scribble.reporting.themes import THEMES as _BUNDLED_THEMES

# The entry-point group an installed package advertises a Theme under — one level down from
# `[project.entry-points."lotek.extensions"]` (see pyproject.toml), same idiom, narrower scope.
REPORT_THEME_ENTRY_POINT_GROUP = "scribble.report_themes"

# This module only ever PRODUCES "installed" (it is the installed-Theme discovery mechanism); "bundled"
# and "override" are the other two Provenances CONTEXT.md names, produced elsewhere (themes.py's own
# registry, and wherever #104's operator-supplied override lands). Spelled out as a constant rather than
# a literal string so every descriptor's tag and every test's assertion refer to the same value.
PROVENANCE_INSTALLED = "installed"


@dataclass(frozen=True)
class InstalledThemeDescriptor:
    """One Theme found via the ``scribble.report_themes`` entry point. Always ``provenance ==
    PROVENANCE_INSTALLED`` — see the module docstring for why that is a real field and not left for a
    caller to infer.

    ``load_toml`` is a zero-argument callable returning the Theme's TOML text (a ``str``) — see the
    module docstring's "Payload contract" section. It is a WRAPPER around the entry point's own
    callable that additionally enforces the ``str`` return type at call time, so a misbehaving
    installed package fails with an immediate, on-the-spot ``TypeError`` naming the offending Theme
    rather than a confusing failure several calls later inside the TOML parser. It is never called by
    this module — see "Why the callable is never invoked here".
    """

    name: str
    provenance: str
    load_toml: Callable[[], str]
    distribution: str | None


@dataclass(frozen=True)
class ThemeLoadError:
    """One ``scribble.report_themes`` entry point that did NOT become a usable descriptor.

    Never dropped — see "Error surfacing" in the module docstring. ``entry_point_name`` is ``ep.name``
    when it was readable at all (an entry point with no name gets the literal ``"<unnamed>"`` so the
    error is still findable in a list rather than keyed on an empty string).
    """

    entry_point_name: str
    distribution: str | None
    error: str


@dataclass(frozen=True)
class ThemeCollision:
    """An installed Theme whose name lost to another Theme of the same name (bundled, or another
    installed package that got there first — see "Name collision policy" in the module docstring).

    The losing Theme is excluded from ``ThemeDiscovery.themes`` entirely; this record is what lets a
    caller surface the collision instead of it vanishing along with the exclusion.
    """

    name: str
    distribution: str | None


@dataclass(frozen=True)
class ThemeDiscovery:
    """The full result of one discovery pass. ``themes`` never contains a name present in ``collisions``
    — see the module docstring's collision policy."""

    # A READ-ONLY mapping, and that is load-bearing rather than fastidious. This result is cached as a
    # per-process singleton, so every caller for the life of the process holds the SAME object; a
    # caller merging bundled Themes in by mutating it in place — the obvious way to write that merge —
    # would corrupt the registry for every later request, not just its own. Provenance also drives the
    # Mark trust gate (#104), so a corrupted mapping is a plausible route to a Theme carrying the wrong
    # trust level. `MappingProxyType` turns that from a silent, permanent corruption into a `TypeError`
    # at the exact line that tried it. Merge by building a NEW dict. (Adversarial review of #103; the
    # hazard did not exist before the cache was added, because each call returned a fresh dict.)
    themes: Mapping[str, InstalledThemeDescriptor]
    errors: tuple[ThemeLoadError, ...] = ()
    collisions: tuple[ThemeCollision, ...] = ()


def _installed_entry_points() -> list[Any]:
    """The real, installed ``scribble.report_themes`` entry points. Isolated as its own function (the
    same seam lotek's own extension discovery uses for `lotek.extensions`) so most tests can instead pass
    ``entry_points=`` straight into :func:`discover_installed_themes` without touching real distribution
    metadata; a test exercising this default path monkeypatches THIS function (or
    ``importlib.metadata.entry_points`` itself) instead. Never raises: a corrupted metadata cache must
    degrade discovery to "found nothing", not crash whatever page wanted the Theme switcher.
    """
    try:
        return list(importlib.metadata.entry_points(group=REPORT_THEME_ENTRY_POINT_GROUP))
    except Exception:  # noqa: BLE001 — discovery must never raise; see module docstring
        return []


def _distribution_name(ep: Any) -> str | None:
    """The pip-installed distribution name behind an entry point, for the error/collision records an
    operator actually needs to go fix something ("which package do I uninstall/rename?"). Best-effort:
    an entry point built by hand (as every test here does) has no real ``dist``, and that is not itself
    an error worth collecting."""
    try:
        dist = getattr(ep, "dist", None)
    except Exception:  # noqa: BLE001
        # `getattr`'s default only substitutes on AttributeError. `.dist` on a real
        # `importlib.metadata.EntryPoint` is a PROPERTY, so a corrupted metadata cache raising
        # anything else from it propagated straight out of discovery — crashing every Theme, bundled
        # included, for every render in the process, over a label used only in an error message.
        # This is the same defence `.name` already had; it was missing here purely by omission.
        # (Found by adversarial review of #103, reproduced live.)
        return None
    if dist is None:
        return None
    try:
        name = dist.name
    except Exception:  # noqa: BLE001 — a malformed Distribution must not break discovery over a label
        return None
    return str(name) if name else None


def _describe_exc(exc: BaseException) -> str:
    """A one-line, log/UI-safe summary of a load failure. The exception's own traceback is the caller's
    to log in full if it wants to; this bounded string is what goes in the returned record so a
    pathological message can't bloat it."""
    return f"{type(exc).__name__}: {exc}".replace("\n", " ").strip()[:500]


def _wrap_load_toml(entry_point_name: str, target: Callable[[], Any]) -> Callable[[], str]:
    """Wrap an entry point's raw callable so calling it LATE (at render time — see "Why the callable is
    never invoked here") enforces the ``str`` payload contract right at the point the bad value first
    appears, instead of letting a dict/``None``/bytes flow on into ``tomllib.loads`` several calls away
    from the actual mistake. The wrapped callable is what ends up on
    :attr:`InstalledThemeDescriptor.load_toml`; the entry point's own callable is never exposed directly.
    """

    def _load_toml() -> str:
        result = target()
        if not isinstance(result, str):
            raise TypeError(
                f"{entry_point_name}: scribble.report_themes entry point must return the Theme's TOML "
                f"text as a str, got {type(result).__name__}"
            )
        return result

    return _load_toml


# Module-level memoisation of the REAL-environment discovery pass — see the module docstring's
# "Caching" section for why a simple lazy singleton is enough (entry points are fixed for the life of
# the process) and why it is only ever populated/consulted for the fully-default call shape.
_cached_discovery: ThemeDiscovery | None = None


def clear_theme_discovery_cache() -> None:
    """Forget the memoised real-environment discovery result.

    Only needed by a test that calls :func:`discover_installed_themes` with both seams left at their
    default (``entry_points=None``, ``bundled_names=None``) more than once against different
    monkeypatched environments — every OTHER call (passing either seam explicitly) never touches the
    cache in the first place. Production code has no reason to call this: the real entry points it
    reads from genuinely do not change within one running process.
    """
    global _cached_discovery
    _cached_discovery = None


def discover_installed_themes(
    *,
    entry_points: Iterable[Any] | None = None,
    bundled_names: Iterable[str] | None = None,
) -> ThemeDiscovery:
    """Discover Themes shipped by separately pip-installed packages.

    ``entry_points`` defaults to the real installed set (:func:`_installed_entry_points`) and is an
    injectable seam so a test can present synthetic entries without installing a package — pass an
    iterable of objects carrying ``.name``, ``.load()``, and optionally ``.dist`` (a real
    ``importlib.metadata.EntryPoint`` satisfies this; so does a small fake).

    ``bundled_names`` defaults to the real bundled registry (``themes.THEMES``) and is likewise
    injectable, so a collision test does not need to know or depend on which names Scribble happens to
    ship bundled today.

    Every entry point is resolved independently — one broken package never stops the rest from being
    discovered. See the module docstring for the full rationale on Provenance, the TOML-text payload
    contract, collisions, error surfacing, and caching.
    """
    use_cache = entry_points is None and bundled_names is None
    global _cached_discovery
    if use_cache and _cached_discovery is not None:
        return _cached_discovery

    eps = _installed_entry_points() if entry_points is None else list(entry_points)
    bundled = frozenset(_BUNDLED_THEMES) if bundled_names is None else frozenset(bundled_names)

    themes: dict[str, InstalledThemeDescriptor] = {}
    errors: list[ThemeLoadError] = []
    collisions: list[ThemeCollision] = []

    # Names are matched CASE-FOLDED throughout, because ``themes.get_theme`` lower-cases before
    # looking up. Without folding here, an installed Theme called "Dark" would not register as
    # colliding with the bundled ``dark`` — and would then be selected by ``?theme=dark`` anyway,
    # which is precisely the surprise re-skin the "bundled always wins" policy exists to prevent.
    # (Seam flagged by adversarial review of #103.)
    bundled_folded = {str(n).strip().lower() for n in bundled}

    for ep in eps:
        dist = _distribution_name(ep)
        try:
            raw_name = getattr(ep, "name", "") or ""
        except Exception:  # noqa: BLE001 — a malformed entry-point object must not break discovery
            raw_name = ""
        name = str(raw_name).strip().lower()
        if not name:
            errors.append(
                ThemeLoadError(
                    entry_point_name="<unnamed>", distribution=dist, error="entry point declared no name"
                )
            )
            continue
        try:
            target = ep.load()
        except Exception as exc:  # noqa: BLE001 — one broken package must not break discovery of the rest
            errors.append(ThemeLoadError(entry_point_name=name, distribution=dist, error=_describe_exc(exc)))
            continue
        if not callable(target):
            errors.append(
                ThemeLoadError(
                    entry_point_name=name,
                    distribution=dist,
                    error=(
                        "entry point did not resolve to a callable TOML-text loader "
                        f"(got {type(target).__name__})"
                    ),
                )
            )
            continue
        if name in bundled_folded or name in themes:
            # Bundled wins outright; between two installed packages the first one `entry_points()`
            # yielded wins. Either way: excluded from `themes`, recorded here — see the module
            # docstring's "Name collision policy".
            collisions.append(ThemeCollision(name=name, distribution=dist))
            continue
        themes[name] = InstalledThemeDescriptor(
            name=name,
            provenance=PROVENANCE_INSTALLED,
            load_toml=_wrap_load_toml(name, target),
            distribution=dist,
        )

    result = ThemeDiscovery(
        themes=MappingProxyType(dict(themes)),
        errors=tuple(errors),
        collisions=tuple(collisions),
    )
    if use_cache:
        _cached_discovery = result
    return result
