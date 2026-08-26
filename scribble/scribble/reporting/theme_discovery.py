"""Discovery of **installed** report Themes — the ones that do NOT ship inside Scribble.

``scribble/CONTEXT.md`` names three Provenances a Theme can carry: **bundled** (ships inside Scribble),
**installed** (a separate package Scribble discovers), and **override** (data an operator supplied at
runtime). This module produces the middle one. A firm that wants its own palette/type/marks on every
report it issues ships a small pip-installable package advertising a
``[project.entry-points."scribble.report_themes"]`` entry — the same idiom this very package uses one
level up to advertise itself to lotek (``[project.entry-points."lotek.extensions"]`` in
``pyproject.toml``) — and Scribble finds it here without a code change or a redeploy of Scribble itself.

**Provenance is not cosmetic.** #104 gates whether a Theme's Marks may be raw SVG (arbitrary markup, so
XSS-capable if it reached the DOM unsanitised) on exactly this value: *installing a package is already
arbitrary code execution*, so a Theme that arrived that way is trusted the way the rest of Scribble's own
code is trusted. An *override* — data an operator pasted into a form at runtime — earns no such trust; it
never ran as code to get here, so its Marks need sanitising (or rejecting) regardless of how convincing
they look. Every :class:`InstalledThemeDescriptor` this module returns carries ``provenance ==
PROVENANCE_INSTALLED`` as a real field for exactly this reason — so a caller deciding "may this Theme's
Marks skip the sanitiser" reads one attribute instead of re-deriving trust from *how it got the object*,
which is the kind of ambient reasoning that is easy to get backwards under a later refactor.

**What "payload" means here is deliberately left open.** The closed Token allowlist lands in #101 and
Marks land in #104; this ticket (#103) is only the discovery *mechanism*, landing first so the shape of
"how Scribble finds a Theme it didn't ship" is settled independently of what a Theme actually contains —
the same reason ``layouts.py``/``themes.py`` split before either grew a token payload. So an entry point's
target is not assumed to be any particular dataclass: it need only be *callable*. ``ep.load()`` (import
+ attribute lookup) happens during discovery, because that is the only way to hand the caller "a
callable" at all — but the callable itself is never INVOKED here. Materialising the actual payload (and
whatever #101/#104 eventually require of its shape) is left to whoever is about to render with this
Theme selected. Two reasons to split it there rather than call it eagerly:

1. Discovery runs on every request that needs the Theme switcher's option list (see ``themes.list_themes``
   for the bundled equivalent); a firm-brand package's construction logic — reading a logo off disk,
   validating a palette — has no business running on every page view of every OTHER report.
2. It keeps this module's own error handling honest: a raise from ``ep.load()`` means "this entry point
   is not resolvable" (a discovery-shaped failure, collected below); a raise from actually building the
   payload is a render-shaped failure and belongs to the caller that triggered it, with the context
   (which report, which request) that only that caller has.

**Name collision policy: bundled wins, and the collision is surfaced, never silent.** An installed Theme
whose name matches a bundled one is EXCLUDED from the returned mapping outright — not merely
shadowed-if-you-merge-in-the-wrong-order — and recorded in ``ThemeDiscovery.collisions`` instead. Argued:
a report is a client deliverable, and the failure mode of "a transitively installed dependency happened to
publish a Theme named `default` and a report silently re-skinned" is worse than the failure mode of "the
firm's installed Theme was refused and an operator has to rename it" — the first is invisible until a
client notices unexpected branding on a delivered PDF; the second is a loud, fixable error at
discover-then-render time. Baking the exclusion into discovery itself (rather than leaving "bundled wins"
as a rule the caller must apply correctly at every merge site) means a future caller cannot get this
backwards by merging the two dicts in the wrong order — the colliding name is simply never IN the returned
mapping to begin with. Two installed packages independently claiming the same name are treated the same
way (first one ``entry_points()`` yields wins, the rest collide) for the identical reason, though note
``entry_points()`` order across distributions is not a documented stable contract, so which one of two
colliding INSTALLED packages wins is best-effort, not a guarantee — the bundled-vs-installed case is the
one that actually matters, because Scribble controls that ordering absolutely.

**Error surfacing.** ``CLAUDE.md`` records a real incident: lotek's own extension discovery swallows every
exception by design, which once made a genuine mounting bug ("a PAT write 403s") baffling to debug because
nothing said an extension had failed to load at all. This module does the opposite on purpose — a broken
installed Theme (an import error in the package, a malformed entry point, a target that is not callable)
is never simply absent from the result. It is collected into ``ThemeDiscovery.errors`` so a caller can log
it or render it on an admin page, while every OTHER entry point is still resolved. Discovery itself never
raises: a corrupted `importlib.metadata` cache degrades to an empty result, not a crashed page.

This module is pure: no Flask, no logging configuration, no I/O beyond `importlib.metadata`. It returns
data and lets the caller decide what to do with it.
"""

from __future__ import annotations

import importlib.metadata
from collections.abc import Callable, Iterable
from dataclasses import dataclass
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
    """

    name: str
    provenance: str
    loader: Callable[[], Any]
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

    themes: dict[str, InstalledThemeDescriptor]
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
    dist = getattr(ep, "dist", None)
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
    discovered. See the module docstring for the full rationale on Provenance, collisions, and error
    surfacing.
    """
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
                    error=f"entry point did not resolve to a callable loader (got {type(target).__name__})",
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
            name=name, provenance=PROVENANCE_INSTALLED, loader=target, distribution=dist
        )

    return ThemeDiscovery(themes=themes, errors=tuple(errors), collisions=tuple(collisions))
