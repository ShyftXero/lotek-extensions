"""Bundled report **Themes** as data files — TOML plus their (optional) web fonts (#102).

``reporting.themes`` gives a Theme an identity and an on-screen stamp (``auto``/``light``/``dark``);
this module gives it a *payload* by loading ``scribble/report_themes/<name>.toml`` off disk. The two
files landing here, ``light.toml`` and ``dark.toml``, are a REFACTOR of the palette values that were
previously hardcoded in ``reporting.render_html``'s ``_CSS`` string into data — not a redesign. Every
colour in them was copied verbatim out of that stylesheet's ``:root`` and ``:root[data-theme="dark"]``
blocks; ``tests/test_report_theme_files.py`` re-parses that same stylesheet and asserts the two never
drift apart.

Why bother, if the values are identical either way? Because a hardcoded palette can only ever be one
of the closed set baked into ``render_html``. A Theme that is DATA can be shipped as a THIRD kind of
thing — see ``CONTEXT.md``'s **Provenance**: bundled (this module), installed (a separate package
Scribble discovers), or override (an operator-supplied file at runtime) — without touching Python at
all. This module only implements the "bundled" case; installed/override provenance gets its own
loader later, reusing the same TOML schema and the pure ``_parse_theme_toml`` below.

## The force-include trap this module deliberately AVOIDS

``CLAUDE.md`` documents a real trap in this repo: ``lotek-extension.toml`` and ``docs/SCRIBBLE.md``
live at the REPO ROOT, outside ``[tool.hatch.build.targets.wheel] packages = ["scribble"]``, so
hatchling's default wheel build never sees them — they need an explicit ``force-include`` stanza to be
copied INTO the built wheel, or the mount manifest is silently absent and extension discovery (which
swallows every exception) just... doesn't mount the extension, with no error anywhere.

``report_themes/`` does NOT have that problem, and this was verified empirically rather than assumed:
this package's directory (``scribble/scribble/report_themes/``) already lives INSIDE the ``scribble``
package tree that ``packages = ["scribble"]`` covers, the same way ``scribble/report_templates/`` (a
binary ``.docx``) and ``scribble/static/`` (``.css``/``.js``) already ship today with no force-include
entry for either. `` uv build --wheel`` was run against this checkout with a throwaway
``report_themes/_probe.toml`` and ``_probe.woff2`` (including one that was NOT ``git add``-ed) and both
came out the other side inside the wheel's ``scribble/report_themes/`` directory untouched. **No
pyproject.toml change is required for this ticket.** If a future reviewer wants an explicit,
belt-and-suspenders record of that intent rather than relying on hatchling's default packaging, the
no-op-today stanza would be::

    [tool.hatch.build.targets.wheel.force-include]
    "scribble/report_themes" = "scribble/report_themes"

— but that line changes nothing observable today; only add it if the team wants the intent spelled out.

``report_themes/`` also carries no ``__init__.py``: it is a resource-only implicit namespace package
(PEP 420), which ``importlib.resources.files("scribble.report_themes")`` resolves correctly — verified
against this checkout's own editable dev install, which is the same API path a real zipimport wheel
uses. This module reads exclusively through ``importlib.resources``, never ``__file__`` arithmetic, for
exactly the reason ``CLAUDE.md`` calls out: ``__file__``-relative paths break under zipimport, and a
broken loader here would fail exactly as silently as a missing force-include — the Theme just wouldn't
exist at runtime, with nothing in the logs pointing at why.

## Schema

Each ``<name>.toml`` has four sections (see ``report_themes/README.md`` for the full spec other Theme
authors should read):

- ``[identity]`` — ``name`` (must equal the filename) and ``label`` (switcher display text).
- ``[tokens]`` — a FLAT ``string -> string`` mapping, run wholesale through
  ``reporting.tokens.validate_tokens`` (#101, landed alongside this ticket): an unknown token name, or
  any single invalid value, rejects the WHOLE file exactly the way it rejects an ``override`` payload —
  a bundled Theme file that typos a colour is exactly as dangerous on the page as an adversarial one
  (see that module's docstring), so it gets no special trust for being data on disk instead of an
  admin-submitted form. ``ThemeFile.tokens`` is therefore always a value that has already passed the
  closed grammar, never a raw echo of what the TOML happened to say.
- ``[fonts]`` — ``embed`` (bool), an optional ``package`` naming where the face files live, plus zero
  or more ``[[fonts.face]]`` tables, each naming a ``.woff2`` file that must be a bare SIBLING filename
  inside that package (see "Font embedding" below). ``package`` defaults to this module's own
  ``report_themes``, which is right for a bundled Theme; an INSTALLED Theme arrives as TOML text with
  no filesystem identity of its own, so it must name its own distribution's resource package or its
  fonts would be looked for in scribble's and silently not found. A face's ``weight`` may be a single
  value or a RANGE (``"300 700"``) for a variable font — Google serves Heebo and Montserrat as one
  file spanning the whole axis, so a range is three files where naming each weight was six.
- ``[marks]`` — the Theme's graphical identity. ``logo_svg`` is SVG source, read here as TEXT and
  bounded, but NOT vetted here: ``reporting.marks.resolve_mark`` is the single gate, it keys off
  Provenance (bundled/installed may carry SVG; an operator-uploaded ``override`` Theme is
  raster-only), and both the write path and the render path call it. Sanitizing at parse time as well
  would create a second opinion about what is acceptable, which is precisely the split that let cream
  ship a renderer more permissive than its own API.

## Font embedding — WHY inline, never a ``<link>``

A rendered report is a self-contained CONFIDENTIAL deliverable: it gets saved as a single ``.html``
file, emailed, and opened later on networks the report author has no visibility into. A
``<link rel="stylesheet" href="https://fonts.googleapis.com/...">`` (or any third-party font host) would
turn EVERY future open of that file into an outbound request — carrying the ``Referer`` of wherever the
file happens to be hosted or opened from — to a party who had nothing to do with the engagement. That
is a data-handling problem for a pentest report in a way it would not be for a public marketing page.
Embedding the font bytes as a ``data:`` URI keeps the report closed: once rendered, it makes no network
requests at all.

No ``.woff2`` file is committed alongside the two BUNDLED Themes — they use the shipped fallback
stack. An installed Theme may well carry real faces (the Synoptek brand Theme carries three).
Every function below therefore MUST treat an absent font file as the normal, expected, non-error state:
``font_data_uri`` returns ``None`` and ``build_font_face_css`` returns ``""``, never an exception, so a
Theme that later grows real font files just starts working without anyone touching the caller.
``embed_fonts`` (``[fonts].embed`` in the TOML) gates the whole feature per-Theme; a Theme can declare
faces without turning embedding on, e.g. while fonts are still being sourced.

``MAX_EMBEDDED_FONT_BYTES`` bounds the TOTAL decoded bytes ``build_font_face_css`` will inline for one
Theme, counted across all its faces. If a face would push the running total over budget, that face is
skipped — never truncated or partially emitted — so the output is always either a complete, valid
``@font-face`` block or nothing for that face; a broken face (one whose ``src`` 404s or whose bytes cut
off mid-file) is worse than no custom face at all, because the browser silently falls back anyway but
now after a failed load instead of never trying.
"""

from __future__ import annotations

import base64
import re
import tomllib
from dataclasses import dataclass, field
from importlib import resources

from scribble.reporting.themes import STAMPS as _STAMPS
from scribble.reporting.tokens import validate_tokens

# The package that carries the bundled Theme files. A plain string (not a module reference) because
# `importlib.resources.files` takes either, and a string keeps this module import-safe even if
# `scribble.report_themes` ever needed to NOT be imported eagerly (it has no `__init__.py` to import
# anyway — see the module docstring's namespace-package note).
_PACKAGE = "scribble.report_themes"

# Total decoded (pre-base64) bytes `build_font_face_css` will inline for one Theme, summed across all
# its declared faces. Base64 inflates this by ~33% again in the actual HTML payload, so treat this as
# a bound on the DECODED size, not the wire size. 512 KiB comfortably covers a couple of full font
# weights at typical woff2 compression while keeping a themed report in the same ballpark as one that
# embeds no fonts at all — see the module docstring's "Font embedding" section for why this exists.
MAX_EMBEDDED_FONT_BYTES = 512 * 1024


class ThemeFileError(ValueError):
    """A bundled Theme file's TOML failed to parse or violates the schema.

    Raised — never swallowed into a partial ``ThemeFile`` — because a file that reaches this state is
    a BUILD defect (a Theme author broke the schema, or a copy-paste left `[identity].name` pointing
    at the wrong file), not a runtime data condition. Contrast `load_theme_file`, which returns `None`
    for an unknown NAME: that is a caller passing through an untrusted string (the same situation
    `themes.get_theme` already resolves safely), not a broken bundled file.
    """


@dataclass(frozen=True)
class FontFace:
    """One ``@font-face`` a Theme declares.

    ``file`` is a bare filename, resolved as a SIBLING of the Theme's own ``.toml`` inside
    ``report_themes/`` via `importlib.resources` — never a filesystem path. `_is_safe_sibling_filename`
    rejects anything containing a path separator or ``..`` at parse time, so a malicious or malformed
    Theme file cannot make font loading read a file outside this package's own directory.
    """

    family: str
    weight: int | str
    style: str
    file: str


@dataclass(frozen=True)
class ThemeFile:
    """A bundled Theme, loaded and schema-checked from ``report_themes/<name>.toml``.

    ``tokens`` has already passed ``reporting.tokens.validate_tokens`` wholesale — see the module
    docstring's "Schema" section — so every value here is guaranteed to satisfy its Token's grammar.
    """

    name: str
    label: str
    tokens: dict[str, str]
    embed_fonts: bool
    faces: tuple[FontFace, ...]
    # Opt-in paper overrides. EMPTY is the safe default: absent these, the base sheet's @media print
    # rule keeps full control of paper, which is what stops a screen-tuned palette reaching a client's
    # printed deliverable. See `_parse_theme_toml` for the incident this guards.
    # Which palette this Theme's values are tuned for, stamped onto <html data-theme>. "" = follow
    # the viewer. See `_parse_theme_toml` for why a light-tuned brand must say so.
    stamp: str = ""
    print_tokens: dict[str, str] = field(default_factory=dict)
    # Where this Theme's font files live, as an importable package name. A BUNDLED Theme's faces are
    # siblings of its own `.toml` inside `report_themes/`, which is the default. An INSTALLED Theme's
    # faces live in ITS OWN distribution — it arrives here as TOML text with no filesystem identity at
    # all, so it has to say where its resources are or `_read_font_bytes` would look for them in
    # scribble's package and silently find nothing.
    font_package: str = _PACKAGE
    # The Theme's logo, as SVG source. Vetting is NOT done here: `reporting.marks.resolve_mark` is the
    # single gate both the write path and the render path call, and it keys off Provenance — an
    # installed or bundled Theme may carry SVG, an operator-uploaded `override` Theme is raster-only.
    # cream shipped a real bug by having its API and its renderer disagree about what was acceptable;
    # one gate, called from both sides, is how that is not repeated.
    logo_svg: str | None = None


def list_theme_files() -> list[str]:
    """Names of every bundled Theme file (sans ``.toml``), sorted for a stable listing.

    Walks `importlib.resources.files(...)` rather than globbing a filesystem path, so this works
    identically from an installed wheel — including a zipimport, where there is no real filesystem
    path to glob at all. See the module docstring's force-include note for why that distinction
    matters in this repo specifically.
    """
    root = resources.files(_PACKAGE)
    names = [
        entry.name.removesuffix(".toml")
        for entry in root.iterdir()
        if entry.is_file() and entry.name.endswith(".toml")
    ]
    return sorted(names)


def load_theme_file(name: str) -> ThemeFile | None:
    """Load and validate one bundled Theme file by name (e.g. ``"light"``, ``"dark"``).

    Returns ``None`` for an unknown or unsafe name (blank, or containing a path separator) — the same
    "resolve safely rather than raise" treatment `themes.get_theme` gives an untrusted `?theme=` value,
    since a Theme NAME reaching this function may equally be caller-controlled. Raises
    `ThemeFileError` for a file that EXISTS but fails to parse or violates the schema: see that
    exception's docstring for why that case is different.
    """
    if not name or "/" in name or "\\" in name or name in (".", ".."):
        return None
    resource = resources.files(_PACKAGE).joinpath(f"{name}.toml")
    if not resource.is_file():
        return None
    text = resource.read_text(encoding="utf-8")
    return _parse_theme_toml(name, text)


def _parse_theme_toml(expected_name: str, text: str) -> ThemeFile:
    """Pure parse + validate step, split out of `load_theme_file` so tests can feed it hand-built TOML
    text directly — proving the malformed/unknown-schema paths doesn't require writing throwaway files
    into the real `report_themes/` package.
    """
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ThemeFileError(f"{expected_name}: invalid TOML: {exc}") from exc

    identity = data.get("identity")
    if not isinstance(identity, dict):
        raise ThemeFileError(f"{expected_name}: missing [identity] section")
    name = identity.get("name")
    label = identity.get("label")
    if not isinstance(name, str) or not name:
        raise ThemeFileError(f"{expected_name}: [identity].name must be a non-empty string")
    if not isinstance(label, str) or not label:
        raise ThemeFileError(f"{expected_name}: [identity].label must be a non-empty string")
    if name != expected_name:
        # Catches a copy-paste Theme file whose filename and declared identity have drifted apart —
        # a mismatch here would otherwise surface much later as a Theme that answers to two names.
        raise ThemeFileError(
            f"{expected_name}: [identity].name={name!r} does not match the filename"
        )
    # Which palette this Theme's values are TUNED FOR, stamped onto <html data-theme>. Not cosmetic:
    # a Theme whose colours were chosen against white must say so, because the base sheet's dark
    # palette is 0-2-0 and the screen override is 0-2-0-but-later, so a light-tuned palette silently
    # wins over dark on a dark-OS viewer. For a Theme that sets every colour that is merely
    # light-on-dark-OS (the brand's own intent for a printed deliverable); for one that sets only
    # SOME, the unset tokens still come from the dark palette and the result is a mix of two palettes
    # — the same defect class `@media print` exists to prevent, on the other medium. Stamping `light`
    # makes `:root:not([data-theme="light"])` stop matching and removes the ambiguity outright.
    # Default "" = auto, i.e. follow the viewer, which is only right for a Theme that re-themes both.
    stamp = identity.get("stamp", "")
    if stamp not in _STAMPS:
        raise ThemeFileError(
            f"{expected_name}: [identity].stamp must be one of {sorted(_STAMPS)} "
            '(default "" means follow the viewer\'s prefers-color-scheme)'
        )

    tokens_raw = data.get("tokens", {})
    # Wholesale gate, not a filter: an unknown key or a single bad value rejects the WHOLE file — see
    # `validate_tokens`'s own docstring for why a partial apply is worse than an outright rejection.
    tokens = validate_tokens(tokens_raw) if isinstance(tokens_raw, dict) else None
    if tokens is None:
        raise ThemeFileError(
            f"{expected_name}: [tokens] failed reporting.tokens' closed allowlist — either it is not "
            "a table, it names a token outside ALLOWED_TOKENS, or one of its values failed that "
            "token's grammar (see reporting/tokens.py)"
        )

    # [print_tokens] is OPT-IN paper theming, and its absence is the safe default. The `@media print`
    # rule in _CSS deliberately forces paper-appropriate values for EVERY colour token, because a
    # dark-mode browser once printed near-white ink and a 1.6:1 accent onto white paper. A Theme's
    # ordinary [tokens] are SCREEN values — the bundled dark Theme's `--sev-high` is #ef8a44, tuned
    # against a near-black panel — so carrying them to paper silently re-breaks that control. (This is
    # not hypothetical: an earlier cut of this integration did exactly that, and
    # test_report_print_media.py::test_a_dark_template_still_prints_on_paper_colours caught it.)
    # A Theme that genuinely wants its brand on paper must therefore state paper values explicitly,
    # having reasoned about contrast on white. Same closed allowlist, same wholesale gate.
    print_tokens_raw = data.get("print_tokens", {})
    if not isinstance(print_tokens_raw, dict):
        raise ThemeFileError(f"{expected_name}: [print_tokens] must be a table")
    print_tokens = validate_tokens(print_tokens_raw)
    if print_tokens is None:
        raise ThemeFileError(
            f"{expected_name}: [print_tokens] failed reporting.tokens' closed allowlist"
        )

    fonts_raw = data.get("fonts", {})
    if not isinstance(fonts_raw, dict):
        raise ThemeFileError(f"{expected_name}: [fonts] must be a table")
    embed_fonts = fonts_raw.get("embed", False)
    if not isinstance(embed_fonts, bool):
        raise ThemeFileError(f"{expected_name}: [fonts].embed must be a bool")
    # Where this Theme's font files live. Defaults to scribble's own `report_themes` package, which is
    # correct for a bundled Theme; an installed Theme must name its own. Validated as a dotted
    # identifier and resolved only through importlib.resources, so it can name a package but can never
    # express a filesystem path — combined with `_is_safe_sibling_filename` on each face, a Theme can
    # reach exactly one directory: the one it declares.
    font_package = fonts_raw.get("package", _PACKAGE)
    if not isinstance(font_package, str) or not _PACKAGE_NAME_RE.fullmatch(font_package):
        raise ThemeFileError(
            f"{expected_name}: [fonts].package must be a dotted importable package name"
        )

    faces_raw = fonts_raw.get("face", [])
    if not isinstance(faces_raw, list):
        raise ThemeFileError(f"{expected_name}: [[fonts.face]] must be an array of tables")
    faces = tuple(_parse_face(expected_name, i, face_raw) for i, face_raw in enumerate(faces_raw))

    # [marks] — a Theme's graphical identity (#104). `logo_svg` is read as SOURCE TEXT and is NOT
    # vetted here: `reporting.marks.resolve_mark` is the one gate, it keys off Provenance, and both the
    # write path and the render path call it. Deliberately not sanitizing at parse time, because that
    # would create a second opinion about what is acceptable — exactly the split that let cream ship a
    # renderer more permissive than its own API. An absent [marks] is entirely normal.
    marks_raw = data.get("marks", {})
    if not isinstance(marks_raw, dict):
        raise ThemeFileError(f"{expected_name}: [marks] must be a table")
    logo_svg = marks_raw.get("logo_svg")
    if logo_svg is not None and not isinstance(logo_svg, str):
        raise ThemeFileError(f"{expected_name}: [marks].logo_svg must be a string of SVG source")
    if isinstance(logo_svg, str) and len(logo_svg) > _MAX_LOGO_SVG_CHARS:
        # Bounded before it ever reaches a parser: an unbounded blob here would be parsed, sanitized
        # and then inlined into a document, so the cheapest place to refuse it is at the door.
        raise ThemeFileError(
            f"{expected_name}: [marks].logo_svg exceeds {_MAX_LOGO_SVG_CHARS} characters"
        )

    return ThemeFile(
        name=name,
        label=label,
        stamp=stamp,
        tokens=tokens,
        embed_fonts=embed_fonts,
        faces=faces,
        print_tokens=print_tokens,
        font_package=font_package,
        logo_svg=logo_svg,
    )


def _parse_face(theme_name: str, index: int, face_raw: object) -> FontFace:
    if not isinstance(face_raw, dict):
        raise ThemeFileError(f"{theme_name}: fonts.face[{index}] must be a table")
    try:
        family = face_raw["family"]
        weight = face_raw["weight"]
        style = face_raw["style"]
        file = face_raw["file"]
    except KeyError as exc:
        raise ThemeFileError(f"{theme_name}: fonts.face[{index}] missing required key {exc}") from exc
    # `family`/`weight`/`style` are spliced STRAIGHT into `_font_face_block`'s CSS text with no further
    # escaping (`family` even sits inside a hand-written double-quoted string), so — same reasoning as
    # `reporting.tokens`'s whole module: this schema is written to be reused for an `override`
    # Theme file later (untrusted, operator-supplied), so it gets that grammar's rigor NOW rather than
    # loosely validated data quietly becoming a CSS injection sink the day provenance changes.
    if not _valid_family(family):
        raise ThemeFileError(
            f"{theme_name}: fonts.face[{index}].family must be 1-{_FAMILY_MAX_LEN} letters/digits/"
            "spaces/hyphens (no quotes, braces, or punctuation that could break out of the CSS string)"
        )
    if not _valid_weight(weight):
        raise ThemeFileError(
            f"{theme_name}: fonts.face[{index}].weight must be an integer 1-1000 (or that same integer "
            f"as a string), or one of {sorted(_WEIGHT_KEYWORDS)}"
        )
    if not isinstance(style, str) or style not in _STYLE_VALUES:
        raise ThemeFileError(
            f"{theme_name}: fonts.face[{index}].style must be one of {sorted(_STYLE_VALUES)}"
        )
    if not _is_safe_sibling_filename(file):
        raise ThemeFileError(
            f"{theme_name}: fonts.face[{index}].file={file!r} must be a bare .woff2 filename with no "
            "path separators — it is resolved as a SIBLING of the theme's own .toml, never a path"
        )
    return FontFace(family=family, weight=weight, style=style, file=file)


# Deliberately restrictive — see `_parse_face`'s comment on why these are held to the same bar as
# `reporting.tokens`'s font-stack grammar even though bundled Theme files are code-reviewed today.
# A brand lockup is a handful of paths. 64 KiB is generous for that and still refuses a blob.
_MAX_LOGO_SVG_CHARS = 64 * 1024

_FAMILY_MAX_LEN = 60
_FAMILY_RE = re.compile(r"[A-Za-z0-9 -]+")
_PACKAGE_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*")
_STYLE_VALUES: frozenset[str] = frozenset({"normal", "italic", "oblique"})
_WEIGHT_KEYWORDS: frozenset[str] = frozenset({"normal", "bold", "bolder", "lighter"})


def _valid_family(value: object) -> bool:
    return (
        isinstance(value, str)
        and 0 < len(value) <= _FAMILY_MAX_LEN
        and _FAMILY_RE.fullmatch(value) is not None
    )


def _valid_weight(value: object) -> bool:
    if isinstance(value, bool):  # bool is an int subclass — exclude before the int check below
        return False
    if isinstance(value, int):
        return 1 <= value <= 1000
    if isinstance(value, str):
        if value in _WEIGHT_KEYWORDS:
            return True
        if value.isdigit():
            return 1 <= int(value) <= 1000
        # A weight RANGE, e.g. "300 700", for a VARIABLE font. Not a nicety: Google serves Heebo and
        # Montserrat as one file spanning the whole weight axis (verified — the 300/400/700 downloads
        # are byte-identical), so without ranges a Theme must declare one face per weight pointing at
        # the SAME file and pay the base64 cost once per declaration. For those two families that was
        # six files and 181 KB instead of three and 83 KB, in a document that already inlines evidence
        # images. Two ascending integers, space-separated — CSS's own `font-weight: <min> <max>` form.
        parts = value.split(" ")
        if len(parts) == 2 and all(p.isdigit() for p in parts):
            lo, hi = int(parts[0]), int(parts[1])
            return 1 <= lo <= hi <= 1000
    return False


def _is_safe_sibling_filename(value: object) -> bool:
    """A `[[fonts.face]].file` value must name a file directly inside `report_themes/`, never escape
    it — this is the only thing standing between a Theme file and reading arbitrary paths off disk.
    """
    if not isinstance(value, str) or not value:
        return False
    if "/" in value or "\\" in value or value in (".", ".."):
        return False
    return value.lower().endswith(".woff2")


def _read_font_bytes(face: FontFace, package: str = _PACKAGE) -> bytes | None:
    """Read one declared face's `.woff2` sibling, or `None` if it is absent or unreadable.

    Absence is the NORMAL state today — see the module docstring's "Font embedding" section. This
    must never raise: every caller above it needs to degrade to the CSS fallback stack rather than
    fail a report render over a font file that simply hasn't been committed yet.
    """
    try:
        resource = resources.files(package).joinpath(face.file)
        if not resource.is_file():
            return None
        return resource.read_bytes()
    except Exception:  # noqa: BLE001
        # Deliberately broader than OSError. `package` can now come from a Theme's own
        # `[fonts].package`, so an installed Theme naming a package that is not importable raises
        # ModuleNotFoundError here — and a missing font must degrade to the fallback stack, never turn
        # `/engagements/<id>/report` into a 500 over a typo in someone's branding.
        return None


def _to_data_uri(raw: bytes) -> str:
    return f"data:font/woff2;base64,{base64.b64encode(raw).decode('ascii')}"


def font_data_uri(face: FontFace, package: str = _PACKAGE) -> str | None:
    """Read `face.file` and return it as a base64 `data:font/woff2;base64,...` URI.

    Returns `None` — never raises — when the file is absent, unreadable, or larger than
    `MAX_EMBEDDED_FONT_BYTES` on its own. A single face over the ceiling can never fit the theme-wide
    budget `build_font_face_css` enforces either, so this checks it eagerly rather than only there.
    """
    raw = _read_font_bytes(face, package)
    if raw is None or len(raw) > MAX_EMBEDDED_FONT_BYTES:
        return None
    return _to_data_uri(raw)


def build_font_face_css(theme: ThemeFile) -> str:
    """Build `@font-face` declarations for a Theme's declared faces, or `""`.

    Returns `""` — the signal to keep whatever fallback font stack the page CSS already has — when
    `theme.embed_fonts` is off, no faces are declared, every face's file is absent (today's normal
    state), or a face would exceed the running `MAX_EMBEDDED_FONT_BYTES` budget. Faces are tried in
    declared order and the budget is consumed as they are accepted, so an early large face can cause a
    later smaller one to still fit or to still be skipped, purely on the order the Theme author wrote
    them in — this is a simple bound, not a bin-packing optimizer.
    """
    if not theme.embed_fonts or not theme.faces:
        return ""
    blocks: list[str] = []
    budget = MAX_EMBEDDED_FONT_BYTES
    for face in theme.faces:
        raw = _read_font_bytes(face, theme.font_package)
        if raw is None or len(raw) > budget:
            continue
        budget -= len(raw)
        blocks.append(_font_face_block(face, _to_data_uri(raw)))
    return "\n".join(blocks)


def _font_face_block(face: FontFace, uri: str) -> str:
    return (
        "@font-face {\n"
        f'  font-family: "{face.family}";\n'
        f"  font-weight: {face.weight};\n"
        f"  font-style: {face.style};\n"
        "  font-display: swap;\n"
        f'  src: url("{uri}") format("woff2");\n'
        "}"
    )
