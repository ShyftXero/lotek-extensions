"""Report **Marks** — a Theme's graphical identity assets (logo, shapes), trust-gated by Provenance.

A Mark is the one part of a Theme where the payload itself is a rendering *language* rather than an
opaque value: a colour Token is four hex digits, but a Mark can be an SVG document — a format with its
own scripting, styling, and remote-fetching surface. That makes a Mark's write path and render path a
genuine security boundary, not just a validation nicety, so this module exists independently of
:mod:`scribble.reporting.themes` (which lands the Token payload in #101) even though a Mark is
conceptually part of a Theme (see ``scribble/CONTEXT.md``).

**Precedent, refined.** ``cream`` (``cream/api.py``'s ``_clean_logo`` + ``cream/render.py``'s
``safe_logo``) already solved half of this problem: accept only an inline ``data:image/...;base64,``
raster URI, never a remote URL, because a remote URL reaching a PDF/HTML render engine turns "render
this document" into a server-side fetch to an attacker-chosen host (SSRF). cream also shipped a REAL
bug worth not repeating (see ``plans/feat-cream-deliverable-engine.md``): the write-time check and the
render-time check were two independently-maintained regexes that quietly drifted — the render-time one
briefly accepted ``image/svg+xml`` after the write-time one had already been tightened to refuse it, so
the "defence in depth" pair's inner layer was the *looser* one. The fix here is structural, not just a
tighter regex: :func:`resolve_mark` is the **one** function both the write path and the render path
call, so there is exactly one gate to keep correct, not two to keep in sync.

**The rule this module adds is a refinement of cream's, not a relaxation of it.** cream had exactly one
trust level (an operator-writable brand record), so "raster only, no SVG, ever" was the whole policy.
Scribble Marks span three Provenances with genuinely different trust:

- ``"bundled"``  — ships inside the Scribble package itself.
- ``"installed"`` — a separate Python package Scribble discovers at import time.
- ``"override"`` — data an operator supplied at runtime (arriving in a later tier).

Installing a Python package is *already* arbitrary code execution — a malicious ``installed`` package
does not need to smuggle anything through an SVG Mark when it could simply run code at import time. So
refusing SVG from ``bundled``/``installed`` would add no real protection while destroying legitimate
crisp, scalable vector marks for every firm that ships or installs a Theme package. An ``override``
Mark is different in kind: it is data submitted by an authenticated-but-untrusted-content caller at
request time, no different in trust from any other operator-supplied field, so it gets cream's original
raster-only rule undiluted. In short: **``bundled``/``installed`` may carry a sanitized SVG Mark;
``override`` is raster-only, cream's rule, exactly.**

Both halves are still validated the same way regardless of Provenance — trusting a package's origin is
not the same as trusting its bytes to be well-formed, and a bundled/installed SVG still goes through
:func:`sanitize_svg_mark` in full. Nothing here executes, fetches, or evaluates anything; a rejected
payload degrades to "no Mark", never an exception and never a network call.

**Where a Mark renders (masthead, cover, toolbar) is not this module's concern.** :func:`resolve_mark`
returns a :class:`ResolvedMark` — safe markup/URI plus what little geometry it was cheap to determine —
and leaves placement to whatever assembles the page.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Literal

# --- Provenance ---------------------------------------------------------------------------------------

# The values scribble/CONTEXT.md's Provenance may take, closed the same way `themes.STAMPS` and
# `layouts.BLOCK_KEYS` are: a tuple, checked with an assert rather than silently substituted. Unlike a
# Theme/Layout *name* (which arrives from an untrusted `?theme=`/`?layout=` query value and therefore
# falls back rather than raising), a Mark's `provenance` is never attacker input — it is the caller's
# own classification of where the Theme came from, computed from a closed, code-defined set. An
# unrecognised value here is a programming error in the caller, not hostile data, so it is asserted the
# same way `ReportTheme`/`ReportLayout` assert their own closed sets rather than guessed at.
PROVENANCES: tuple[str, ...] = ("bundled", "installed", "override")

# The only Provenance forbidden from carrying an SVG Mark. See the module docstring for why the other
# two may.
_SVG_FORBIDDEN_PROVENANCES: frozenset[str] = frozenset({"override"})


# --- raster marks --------------------------------------------------------------------------------------

# Same four raster formats cream trusts (`cream/api.py::_LOGO_RE`, `cream/render.py::_LOGO_PREFIX_RE`).
# Deliberately excludes `svg+xml` — that is the entire point of a *raster* mark function; SVG has its own
# gate below, still subject to the Provenance rule. The base64 body's character class
# (`[A-Za-z0-9+/=\s]+`) cannot contain `"`, `'`, `<`, `>`, or a bare scheme like `javascript:` — so unlike
# cream's two-function version, one regex is both the acceptance test and the escaping proof; there is no
# second, differently-shaped check to drift out of sync with this one.
_RASTER_MARK_RE = re.compile(r"^data:image/(png|jpeg|jpg|gif|webp);base64,[A-Za-z0-9+/=\s]+$")

# ~1.5 MB decoded, matching cream's `_MAX_LOGO_CHARS` — generous for a logo, not for an exfiltration
# channel disguised as branding.
MAX_RASTER_MARK_CHARS = 2_000_000


def clean_raster_mark(value: str | None) -> str | None:
    """A vetted ``data:image/(png|jpeg|jpg|gif|webp);base64,...`` URI, or ``None``.

    This is cream's rule (``cream/api.py::_clean_logo``), unchanged: no remote URL, no protocol-relative
    URL, no SVG. Anything that is not an exact match — including a value that merely starts right but
    trails garbage, or one that is technically valid but too long — is dropped. This function never
    raises and never performs I/O; a caller that hands it a remote URL gets ``None`` back, never a
    fetch. Legal under every Provenance (see module docstring): raster is never the weak link.
    """
    if value in (None, ""):
        return None
    candidate = str(value).strip()
    # Length check first: `len()` is O(1) on a str and this order means an oversized payload is
    # rejected without the regex engine ever walking it, however cheap that walk would be.
    if len(candidate) > MAX_RASTER_MARK_CHARS or not _RASTER_MARK_RE.match(candidate):
        return None
    return "".join(candidate.split())  # collapse the whitespace the regex tolerated inside the body


# --- SVG marks -----------------------------------------------------------------------------------------

# A brand mark is small: an icon plus a wordmark, both flattened to path outlines by whatever vector tool
# exported them. This allowlist covers exactly that shape and nothing else:
#   svg    — the document root; mandatory.
#   g      — grouping/transform container; near-universal in exported icon SVGs.
#   path   — the primitive real-world exports use almost exclusively (vector tools flatten shapes to
#            path outlines on export, including wordmark letterforms — a Mark is never expected to carry
#            live <text>, which would drag in font/system-rendering variance this module has no interest
#            in resolving).
#   circle, rect, polygon — the remaining basic shape primitives a hand-authored (non-flattened) mark may
#            use directly instead of an equivalent path.
#   title  — the SVG accessible-name element; keeping it costs nothing and is worth it for a11y.
#   defs   — some export pipelines wrap even a simple mark in an (unreferenced) <defs>. Nothing in this
#            allowlist can ever *reference* a defs child (no <use>, no url(#id) paint servers — see the
#            attribute rule below), so a <defs> subtree never renders regardless of what survives inside
#            it. Keeping the element costs nothing security-wise and avoids rejecting an otherwise
#            legitimate file over an inert wrapper.
# Everything else — script, style, use, image, foreignObject, animate, set, and any node type this
# allowlist does not name — is dropped with its whole subtree, no hoisting: the same "unknown means gone,
# children included" rule `prosemirror_sanitize.py` uses for ProseMirror nodes it does not trust.
ALLOWED_SVG_ELEMENTS: frozenset[str] = frozenset(
    {"svg", "g", "path", "circle", "rect", "polygon", "title", "defs"}
)

# Exactly the presentation/geometry attributes needed to reproduce a solid-colour vector mark: `d` is
# path geometry; fill/stroke/stroke-width/opacity/fill-rule/clip-rule are paint; transform positions a
# group; viewBox/width/height size the document. Deliberately excluded: `style` (would reintroduce CSS,
# including `url(...)`, through a side door), `id`/`class` (a spliced-inline mark sharing a page with
# other marks/CSS should not be able to collide with or be targeted by either), and any `href`/
# `xlink:href` (the reference vector `<use>`/`<image>` rely on — moot here since neither element is
# allowed, but excluded from the attribute list too so a future allowlisted element does not silently
# inherit a reference capability nobody reviewed).
ALLOWED_SVG_ATTRS: frozenset[str] = frozenset(
    {
        "viewBox",
        "d",
        "fill",
        "stroke",
        "stroke-width",
        "transform",
        "width",
        "height",
        "opacity",
        "fill-rule",
        "clip-rule",
    }
)

# An attribute VALUE containing either of these is dropped regardless of the attribute's name. `fill`/
# `stroke` are legal *paint* attributes, and SVG paint values may legitimately be `url(#localGradientId)`
# — but the same syntax also accepts `url(https://attacker/track.svg#x)`, which a renderer will happily
# fetch: the raster SSRF concern again, reachable through an allowlisted attribute rather than a
# forbidden element. `javascript:` is checked for the same reason link `href`s are checked in
# `prosemirror_sanitize.py`: an allowlisted attribute is still just a string, and nothing stops a future
# edit from allowlisting one (e.g. a hypothetical `xlink:href` re-add) that a scheme check would catch.
_BAD_ATTR_VALUE_SUBSTRINGS: tuple[str, ...] = ("url(", "javascript:")

# `defs` -> `g` -> `path` is 3 deep for a typical export; 32 is generous headroom for a hand-nested mark
# while still triggering long before Python's own default recursion limit (1000) on this module's
# recursive walker. Depth is capped independently of MAX_SVG_MARK_CHARS because a byte-cap alone still
# permits a surprisingly deep (if narrow) tree.
_MAX_SVG_DEPTH = 32

# Generous for a two-path brand mark (real exports are typically a few KB) while keeping the worst-case
# element count — and so the cost of walking it — small. Not chosen to defend against entity expansion;
# that is refused outright below, independent of size.
MAX_SVG_MARK_CHARS = 200_000

# Rejecting an SVG payload if either substring appears ANYWHERE in it, case-insensitively, before it is
# ever handed to a parser. This is deliberately blunt — checked against the *raw* string, not the parsed
# tree.
#
# Verified empirically against this exact stdlib (see the ticket's test file): `xml.etree.ElementTree`
# parses a <!DOCTYPE ...> internal subset and DOES expand the entities it defines — a 5-level, 10x-per-
# level "billion laughs" document parses without error or complaint into a fully-expanded string. The
# per-Python docs table (https://docs.python.org/3/library/xml.html#xml-vulnerabilities) confirms
# `xml.etree.ElementTree` is listed vulnerable to both billion-laughs and quadratic-blowup entity
# expansion. There is no `defusedxml` dependency available to this module (pure stdlib only per the
# extension's dependency policy), so the fix here is not a parser flag — it is never letting a DOCTYPE
# reach the parser at all: with no DOCTYPE, expat has no ENTITY declarations to expand (only the five
# built-in XML entities and numeric character references survive, neither of which can recurse), which
# closes billion-laughs, quadratic blowup, AND external-entity/DTD SSRF in one check, since all three
# require a DOCTYPE to declare the entity or external subset in the first place.
_FORBIDDEN_SVG_MARKUP: tuple[str, ...] = ("<!doctype", "<!entity")


def _local_name(tag: str) -> str:
    """Strip a Clark-notation namespace off an ElementTree tag/attribute name (``{uri}local`` -> local).

    `ET.fromstring` resolves a declared `xmlns`/`xmlns:xlink` into this form automatically, so an
    attacker cannot dodge the allowlist by wrapping a payload in an unexpected namespace prefix: `<x:use
    xmlns:x="...">` and a bare `<use>` both reduce to the local name `"use"`, which is what every
    allowlist check below compares against. Verified empirically (see the ticket's test file) that an
    `xlink:href` attribute on a namespaced document reduces to local name `"href"` exactly this way, so
    it is rejected by the attribute allowlist with no special-casing for the `xlink` prefix at all.
    """
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _clean_svg_attr_value(value: str) -> str | None:
    lowered = value.lower()
    if any(bad in lowered for bad in _BAD_ATTR_VALUE_SUBSTRINGS):
        return None
    return value


def _clean_svg_element(el: ET.Element, depth: int) -> ET.Element | None:
    """Return a freshly-built, sanitized copy of one element, or ``None`` to drop it (subtree included).

    Builds a NEW tree rather than mutating the parsed one in place, mirroring
    `prosemirror_sanitize._clean_node`'s shape: the output can only ever contain what this function
    explicitly copied across, so there is no risk of an attribute or child surviving because some later
    pass forgot to strip it.
    """
    if depth > _MAX_SVG_DEPTH:
        return None  # over-deep subtree: drop it whole, same "clean removal, not a crash" choice as
        # `prosemirror_sanitize._clean_node`'s depth cap.
    name = _local_name(el.tag)
    if name not in ALLOWED_SVG_ELEMENTS:
        return None  # unknown/forbidden element: drop it AND its whole subtree, no hoisting — a
        # forbidden <script> or <foreignObject>'s children are exactly as untrusted as it is.

    clean = ET.Element(name)
    for attr_name, attr_value in el.attrib.items():
        local = _local_name(attr_name)
        if local not in ALLOWED_SVG_ATTRS:
            continue  # covers every on* handler and href/xlink:href: neither is ever in the allowlist
        safe_value = _clean_svg_attr_value(attr_value)
        if safe_value is not None:
            clean.set(local, safe_value)

    if name == "title" and el.text:
        clean.text = el.text.strip()[:200]  # the accessible label; clamped defensively, not load-bearing

    for child in el:
        cleaned_child = _clean_svg_element(child, depth + 1)
        if cleaned_child is not None:
            clean.append(cleaned_child)
    return clean


def sanitize_svg_mark(svg: str | None) -> str | None:
    """A sanitized ``<svg>…</svg>`` string safe to splice as INLINE HTML markup, or ``None``.

    Real XML parse (``xml.etree.ElementTree``), not regex — a regex allowlist over markup is exactly the
    kind of check a crafted-but-technically-different-looking payload slips past. The parsed tree is
    walked into a brand-new tree (:func:`_clean_svg_element`) containing only allowlisted elements and
    attributes, then re-serialized; the original parsed tree is discarded, so nothing about the input's
    shape survives except what this function explicitly decided to keep.

    Gate order matters and is deliberate:

    1. type/blank/length check — an oversized payload is rejected without being parsed at all;
    2. a raw-string scan for ``<!DOCTYPE``/``<!ENTITY`` (case-insensitive) — rejected before parsing, for
       the entity-expansion reasons documented above `_FORBIDDEN_SVG_MARKUP`;
    3. parse, then require the root element's local name to be ``svg`` — a bare `<path>` fragment is not
       something safe to splice as a standalone Mark, so it is refused the same as any other bad input;
    4. walk the tree through the allowlist.

    Returns ``None`` — never raises — for anything that fails any of the above, including a malformed
    document `xml.etree.ElementTree` itself refuses to parse.

    Whether this function should even be CALLED depends on Provenance (SVG is refused outright under
    ``"override"``) — that gate lives in :func:`resolve_mark`, the one function both the write path and
    the render path must call, so this function stays a pure "is this SVG safe" check with no notion of
    trust level of its own.
    """
    if not isinstance(svg, str):
        return None
    candidate = svg.strip()
    if not candidate or len(candidate) > MAX_SVG_MARK_CHARS:
        return None
    lowered = candidate.lower()
    if any(bad in lowered for bad in _FORBIDDEN_SVG_MARKUP):
        return None
    try:
        root = ET.fromstring(candidate)
    except ET.ParseError:
        return None
    if _local_name(root.tag) != "svg":
        return None
    cleaned = _clean_svg_element(root, depth=0)
    if cleaned is None:
        return None
    return ET.tostring(cleaned, encoding="unicode")


# A leading run of digits, with an optional decimal part — tolerates a trailing CSS-style unit (`"24px"`
# matches `"24"`) but is not a general CSS-length parser. This is used only for the SVG intrinsic-size
# hint below, which is optional metadata for placement, never a security decision, so a value this
# cannot parse (scientific notation, a percentage, a `calc()`) degrades to `None`, not a crash.
_LEADING_NUMBER_RE = re.compile(r"^(\d+(?:\.\d+)?)")

# A sanity ceiling for the intrinsic-size hint. Not a validation of the SVG (already sanitized by the
# time this runs) — just a refusal to hand a nonsense number (e.g. a many-digit value that overflowed to
# `inf` on `float()`, which `> _MAX_DIMENSION` still catches) on to a caller that might use it verbatim in
# a `width:` CSS declaration.
_MAX_DIMENSION = 100_000


def _parse_dimension(raw: str | None) -> float | None:
    if raw is None:
        return None
    match = _LEADING_NUMBER_RE.match(raw.strip())
    return float(match.group(1)) if match else None


def _clamp_dimension(value: float | None) -> int | None:
    if value is None or value <= 0 or value > _MAX_DIMENSION:
        return None
    return int(value)


def _svg_intrinsic_size(svg_markup: str) -> tuple[int | None, int | None]:
    """Best-effort ``(width, height)`` read off an ALREADY-sanitized SVG's root attributes, or ``(None,
    None)``. Re-parses the sanitized string rather than threading the tree out of `sanitize_svg_mark` —
    the string is this module's own trusted output at this point (no DOCTYPE, no disallowed element ever
    reaches it), so a second parse costs a little and risks nothing.
    """
    try:
        root = ET.fromstring(svg_markup)
    except ET.ParseError:
        return (None, None)
    width = _parse_dimension(root.get("width"))
    height = _parse_dimension(root.get("height"))
    if width is None or height is None:
        view_box = (root.get("viewBox") or "").replace(",", " ").split()
        if len(view_box) == 4:
            if width is None:
                width = _parse_dimension(view_box[2])
            if height is None:
                height = _parse_dimension(view_box[3])
    return _clamp_dimension(width), _clamp_dimension(height)


# --- resolution ----------------------------------------------------------------------------------------

MarkKind = Literal["raster", "svg"]


@dataclass(frozen=True)
class ResolvedMark:
    """A Mark that has already passed :func:`resolve_mark`'s Provenance-gated validation.

    ``kind`` tells a caller which shape ``value`` is, so it never has to re-sniff the content:

    - ``"raster"`` — ``value`` is a vetted ``data:image/...;base64,...`` URI for an attribute context,
      e.g. ``<img src="{value}">`` (still HTML-attribute-escape it on the way in — the allowed charset
      excludes ``"``/``'``/``<``/``>`` so there is nothing to escape in practice, but this dataclass
      makes no promise about what a future consumer splices it into).
    - ``"svg"`` — ``value`` is the complete, sanitized ``<svg>…</svg>`` element markup, safe to splice
      directly as INLINE HTML. It is markup, not a URI — do not wrap it in a ``src=`` attribute.

    ``width``/``height`` are the Mark's intrinsic size in pixels where it was cheap to determine, else
    ``None`` (meaning "unknown", not "zero"). For ``"svg"`` this comes straight off the sanitized
    document's own ``width``/``height``/``viewBox``. For ``"raster"`` this is deliberately always
    ``None`` — decoding a PNG/JPEG/GIF/WEBP header is a parsing surface this module does not take on for
    a placement hint; a caller that needs to lay out a box around a raster Mark should size its
    container from CSS, not from this field.

    Placement — masthead vs. cover vs. toolbar — is not this dataclass's concern; it hands back only
    what is safe plus what it can cheaply say about geometry.
    """

    kind: MarkKind
    value: str
    width: int | None
    height: int | None


def resolve_mark(payload: str | None, *, provenance: str) -> ResolvedMark | None:
    """The ONE function the write path and the render path both call to decide what Mark is safe to use.

    This is the whole fix for the cream bug documented at the top of this module: there is exactly one
    place the Provenance rule is expressed, so a write-time acceptance and a render-time acceptance
    cannot independently drift out of sync the way ``_clean_logo``/``safe_logo`` did. Whatever stores a
    Mark should call this before persisting it; whatever renders one should call this again on whatever
    was persisted (or on a frozen Snapshot's value) rather than trusting that stored data is still safe
    to splice — the render path re-checking is exactly cream's "belt and braces" instinct, kept, just
    pointed at one function instead of two.

    Detection is by validation outcome, not a caller-supplied ``kind`` flag: :func:`clean_raster_mark` is
    tried first (legal under every Provenance), and only if that fails is :func:`sanitize_svg_mark` even
    attempted — and only when ``provenance`` permits SVG at all. A caller-supplied "this is SVG" flag
    would be exactly the kind of second, independently-trusted signal that let cream's two checks
    disagree; asking each validator "is this yours?" and believing the one that says yes removes that
    seam entirely.

    ``provenance`` must be one of :data:`PROVENANCES`; anything else is an ``AssertionError`` (see
    :data:`PROVENANCES`'s docstring on why this asserts rather than degrading — it is not attacker
    input). ``payload`` IS treated as attacker/operator input: anything that fails validation, of any
    type or shape, degrades to ``None``. This function never raises for a bad ``payload`` and never
    performs network I/O.
    """
    assert provenance in PROVENANCES, f"unknown provenance {provenance!r}"

    if payload is None:
        return None

    raster = clean_raster_mark(payload)
    if raster is not None:
        return ResolvedMark(kind="raster", value=raster, width=None, height=None)

    if provenance in _SVG_FORBIDDEN_PROVENANCES:
        # Refused outright: an `override` Mark never even reaches the XML parser. This is the specific
        # branch the ticket's "positive control" test targets — remove this check and a clean SVG payload
        # under `override` would fall through to `sanitize_svg_mark` below and succeed.
        return None

    svg = sanitize_svg_mark(payload)
    if svg is None:
        return None
    width, height = _svg_intrinsic_size(svg)
    return ResolvedMark(kind="svg", value=svg, width=width, height=height)
