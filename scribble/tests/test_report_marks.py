"""Report Marks (#104): raster/SVG validation, and the Provenance rule gating which is trusted.

Covers `scribble.reporting.marks`:

- `clean_raster_mark` — cream's rule verbatim: only an inline raster `data:image/...;base64,` URI, never
  a remote/protocol-relative URL, never SVG.
- `sanitize_svg_mark` — a closed element/attribute allowlist, hostile constructs stripped, a DOCTYPE/
  ENTITY payload refused outright (never handed to the parser), oversize payloads refused before being
  parsed, and a real two-path brand mark surviving intact.
- `resolve_mark` — the one function applying the Provenance rule: `bundled`/`installed` may carry a
  sanitized SVG Mark, `override` is raster-only, cream's rule unrelaxed.

This module is adversarial by design (a security surface), so most tests assert the ABSENCE of a
dangerous token from the sanitizer's output, not just that *some* output was produced.
"""

from __future__ import annotations

import time
from unittest.mock import patch

import pytest

from scribble.reporting.marks import (
    MAX_RASTER_MARK_CHARS,
    MAX_SVG_MARK_CHARS,
    PROVENANCES,
    ResolvedMark,
    clean_raster_mark,
    resolve_mark,
    sanitize_svg_mark,
)

_TINY_PNG = (
    "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
    "+A8AAQUBAScY42YAAAAASUVORK5CYII="
)

_TWO_PATH_LOGO = """<svg viewBox="0 0 64 24" xmlns="http://www.w3.org/2000/svg">
  <title>Acme Corp</title>
  <g transform="translate(0,0)">
    <path d="M2 2 L22 2 L12 20 Z" fill="#0f766e" fill-rule="evenodd"/>
  </g>
  <path d="M28 4 H60 V8 H28 Z M28 12 H52 V16 H28 Z" fill="#14202b" stroke="none"/>
</svg>"""


# --- clean_raster_mark ---------------------------------------------------------------------------------

def test_raster_mark_accepts_a_vetted_png_data_uri():
    assert clean_raster_mark(_TINY_PNG) == _TINY_PNG


# Real magic bytes per format. The payloads used to be a uniform `QQ==` (base64 of b"A"), which is not
# an image in ANY format — the old gate only ever compared the caller's own `image/<fmt>` label against
# itself, so a stand-in that decoded to one letter passed as all five.
_MAGIC_B64 = {
    "png": "iVBORw0KGgo=",       # \x89PNG\r\n\x1a\n
    "jpeg": "/9j/4A==",           # \xff\xd8\xff\xe0
    "jpg": "/9j/4A==",
    "gif": "R0lGODlh",            # GIF89a
    "webp": "UklGRgAAAABXRUJQ",   # RIFF<len>WEBP
}


def test_raster_mark_accepts_every_cream_format():
    for fmt in ("png", "jpeg", "jpg", "gif", "webp"):
        uri = f"data:image/{fmt};base64,{_MAGIC_B64[fmt]}"
        assert clean_raster_mark(uri) == uri


def test_raster_mark_refuses_a_payload_relabelled_as_a_raster_format():
    """The declared `image/<fmt>` is the submitter's claim, so it must be checked against the bytes.

    An SVG document base64'd under an `image/png` label is the whole way around the "an `override`
    Mark is raster-only" rule: `clean_raster_mark` returns it as raster, so `resolve_mark` never even
    reaches the Provenance branch that would refuse SVG. Neutralise the magic-byte check in
    `_decoded_matches_declared_format` (return True) and this test goes red.
    """
    import base64

    svg = b'<svg xmlns="http://www.w3.org/2000/svg" onload="fetch(1)"/>'
    relabelled = "data:image/png;base64," + base64.b64encode(svg).decode()
    assert clean_raster_mark(relabelled) is None

    for fmt in ("png", "jpeg", "gif", "webp"):
        # every other format's magic under a png label, and vice versa
        wrong = f"data:image/{fmt};base64,{_MAGIC_B64['png' if fmt != 'png' else 'gif']}"
        assert clean_raster_mark(wrong) is None


def test_raster_mark_refuses_a_body_that_is_not_really_base64():
    """The regex charset admits `A=B=C`; `b64decode(validate=True)` is what actually refuses it."""
    assert clean_raster_mark("data:image/png;base64,A=B=C") is None


def test_raster_mark_rejects_remote_http_url():
    assert clean_raster_mark("https://evil.example/logo.png") is None


def test_raster_mark_rejects_remote_http_url_even_with_data_uri_looking_suffix():
    assert clean_raster_mark("https://evil.example/data:image/png;base64,QQ==") is None


def test_raster_mark_rejects_protocol_relative_url():
    assert clean_raster_mark("//evil.example/logo.png") is None


def test_raster_mark_rejects_svg_data_uri():
    """The exact shape cream's render-time check once let through by accident — see the module
    docstring's account of the mismatch bug. Both the base64 form and the plain-text form are checked."""
    assert clean_raster_mark("data:image/svg+xml;base64,PHN2Zz48L3N2Zz4=") is None
    assert clean_raster_mark("data:image/svg+xml,<svg></svg>") is None


def test_raster_mark_rejects_none_and_empty():
    assert clean_raster_mark(None) is None
    assert clean_raster_mark("") is None


def test_raster_mark_rejects_garbage():
    assert clean_raster_mark("not a data uri at all") is None
    assert clean_raster_mark("javascript:alert(1)") is None
    assert clean_raster_mark("data:text/html;base64,QQ==") is None


def test_raster_mark_strips_internal_whitespace():
    spaced = "data:image/png;base64,iVBORw0K \n Ggo="
    cleaned = clean_raster_mark(spaced)
    assert cleaned is not None
    assert " " not in cleaned and "\n" not in cleaned


def test_raster_mark_rejects_oversize_payload():
    huge = "data:image/png;base64," + ("A" * (MAX_RASTER_MARK_CHARS + 1))
    assert clean_raster_mark(huge) is None


# --- sanitize_svg_mark: hostile constructs stripped -----------------------------------------------------

def test_svg_mark_strips_script_element():
    svg = '<svg viewBox="0 0 10 10"><script>alert(1)</script><path d="M0 0 L1 1"/></svg>'
    out = sanitize_svg_mark(svg)
    assert out is not None
    assert "script" not in out.lower()
    assert "alert" not in out


def test_svg_mark_strips_onload_handler():
    svg = '<svg viewBox="0 0 10 10" onload="alert(1)"><path d="M0 0 L1 1"/></svg>'
    out = sanitize_svg_mark(svg)
    assert out is not None
    assert "onload" not in out.lower()
    assert "alert" not in out


def test_svg_mark_strips_use_with_href():
    svg = '<svg viewBox="0 0 10 10"><defs><path id="p" d="M0 0 L1 1"/></defs><use href="#p"/></svg>'
    out = sanitize_svg_mark(svg)
    assert out is not None
    assert "use" not in out.lower()
    assert "href" not in out.lower()


def test_svg_mark_strips_image_with_remote_href():
    svg = (
        '<svg xmlns:xlink="http://www.w3.org/1999/xlink" viewBox="0 0 10 10">'
        '<image xlink:href="http://evil.example/x.png" width="10" height="10"/></svg>'
    )
    out = sanitize_svg_mark(svg)
    assert out is not None
    assert "image" not in out.lower()
    assert "evil.example" not in out


def test_svg_mark_strips_foreignobject():
    svg = '<svg viewBox="0 0 10 10"><foreignObject><body xmlns="http://www.w3.org/1999/xhtml">' \
        "<script>alert(1)</script></body></foreignObject></svg>"
    out = sanitize_svg_mark(svg)
    assert out is not None
    assert "foreignobject" not in out.lower()
    assert "script" not in out.lower()


def test_svg_mark_strips_style_with_url():
    svg = (
        '<svg viewBox="0 0 10 10"><style>path{fill:url(http://evil.example/x)}</style>'
        '<path d="M0 0 L1 1"/></svg>'
    )
    out = sanitize_svg_mark(svg)
    assert out is not None
    assert "style" not in out.lower()
    assert "evil.example" not in out


def test_svg_mark_strips_xlink_href_generally():
    svg = (
        '<svg xmlns:xlink="http://www.w3.org/1999/xlink" viewBox="0 0 10 10">'
        '<path xlink:href="http://evil.example" d="M0 0 L1 1"/></svg>'
    )
    out = sanitize_svg_mark(svg)
    assert out is not None
    assert "xlink" not in out.lower()
    assert "evil.example" not in out


def test_svg_mark_strips_animate_and_set():
    svg = (
        '<svg viewBox="0 0 10 10"><path d="M0 0 L1 1">'
        '<animate attributeName="x" to="1"/><set attributeName="y" to="1"/></path></svg>'
    )
    out = sanitize_svg_mark(svg)
    assert out is not None
    assert "animate" not in out.lower()
    assert "<set" not in out.lower()


def test_svg_mark_strips_fill_url_ssrf_attempt():
    """`fill`/`stroke` are allowlisted attributes, but their VALUE can itself be `url(...)` — an SVG
    paint server reference a renderer will fetch. The attribute must be dropped even though its name is
    on the allowlist."""
    svg = '<svg viewBox="0 0 10 10"><path d="M0 0 L1 1" fill="url(http://evil.example/x.svg#p)"/></svg>'
    out = sanitize_svg_mark(svg)
    assert out is not None
    assert "url(" not in out.lower()
    assert "evil.example" not in out


def test_svg_mark_strips_javascript_scheme_in_attribute_value():
    svg = '<svg viewBox="0 0 10 10"><path d="M0 0 L1 1" fill="javascript:alert(1)"/></svg>'
    out = sanitize_svg_mark(svg)
    assert out is not None
    assert "javascript:" not in out.lower()


# --- sanitize_svg_mark: entity expansion / DoS -----------------------------------------------------------

def test_svg_mark_rejects_doctype_billion_laughs_without_expanding():
    billion_laughs = """<?xml version="1.0"?>
<!DOCTYPE svg [
 <!ENTITY lol "lol">
 <!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">
 <!ENTITY lol3 "&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;">
 <!ENTITY lol4 "&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;">
 <!ENTITY lol5 "&lol4;&lol4;&lol4;&lol4;&lol4;&lol4;&lol4;&lol4;&lol4;&lol4;">
]>
<svg viewBox="0 0 10 10">&lol5;</svg>
"""
    start = time.monotonic()
    out = sanitize_svg_mark(billion_laughs)
    elapsed = time.monotonic() - start
    assert out is None
    assert elapsed < 2.0  # never hangs
    # A bare ENTITY declaration outside any DOCTYPE (invalid XML on its own, but cheap to check anyway).
    assert sanitize_svg_mark('<!ENTITY x "y"><svg viewBox="0 0 1 1"/>') is None


def test_svg_mark_rejects_doctype_case_insensitively():
    assert sanitize_svg_mark('<!DoCtYpE svg><svg viewBox="0 0 1 1"/>') is None


def test_svg_mark_rejects_oversize_payload_without_parsing():
    """An oversize payload must be refused by the length check, not merely refused eventually -- it must
    never reach the XML parser at all (`ET.fromstring` is mocked and asserted un-called)."""
    huge = '<svg viewBox="0 0 1 1">' + ("<g>" * (MAX_SVG_MARK_CHARS)) + "</svg>"
    assert len(huge) > MAX_SVG_MARK_CHARS
    with patch("scribble.reporting.marks.ET.fromstring") as fromstring:
        out = sanitize_svg_mark(huge)
    assert out is None
    fromstring.assert_not_called()


# --- sanitize_svg_mark: a real logo survives ------------------------------------------------------------

def test_svg_mark_preserves_a_legitimate_two_path_brand_logo():
    """The sanitizer must not be so strict it destroys a real, benign logo: an icon path plus a
    wordmark path, fill colours, and a viewBox all need to survive intact."""
    out = sanitize_svg_mark(_TWO_PATH_LOGO)
    assert out is not None
    assert out.startswith("<svg")
    assert 'viewBox="0 0 64 24"' in out
    assert "Acme Corp" in out  # <title> text preserved
    assert "M2 2 L22 2 L12 20 Z" in out  # icon path geometry intact
    assert "M28 4 H60 V8 H28 Z M28 12 H52 V16 H28 Z" in out  # wordmark path geometry intact
    assert "#0f766e" in out and "#14202b" in out  # both fill colours survived
    assert out.count("<path") == 2


def test_svg_mark_rejects_non_svg_root():
    assert sanitize_svg_mark('<path d="M0 0 L1 1"/>') is None


def test_svg_mark_rejects_malformed_xml():
    assert sanitize_svg_mark("<svg><path d=") is None


def test_svg_mark_rejects_non_string_input():
    assert sanitize_svg_mark(None) is None
    assert sanitize_svg_mark(12345) is None  # type: ignore[arg-type]


# --- resolve_mark: the Provenance rule -------------------------------------------------------------------

def test_resolve_mark_raster_allowed_under_every_provenance():
    for provenance in PROVENANCES:
        resolved = resolve_mark(_TINY_PNG, provenance=provenance)
        assert resolved == ResolvedMark(kind="raster", value=_TINY_PNG, width=None, height=None)


def test_resolve_mark_svg_accepted_under_bundled():
    resolved = resolve_mark(_TWO_PATH_LOGO, provenance="bundled")
    assert resolved is not None
    assert resolved.kind == "svg"
    assert resolved.value is not None and resolved.value.startswith("<svg")


def test_resolve_mark_svg_accepted_under_installed():
    resolved = resolve_mark(_TWO_PATH_LOGO, provenance="installed")
    assert resolved is not None
    assert resolved.kind == "svg"


def test_resolve_mark_svg_rejected_outright_under_override():
    """THE PLANTED POSITIVE CONTROL. `_TWO_PATH_LOGO` is a genuinely clean, sanitizable SVG — the ONLY
    reason this must come back `None` is the Provenance gate in `resolve_mark`. If that gate were ever
    deleted (falling through to `sanitize_svg_mark` unconditionally), this exact input would sanitize
    successfully and this assertion would fail — that is the point: this test cannot pass by accident,
    only by the gate actually running."""
    assert resolve_mark(_TWO_PATH_LOGO, provenance="override") is None


def test_resolve_mark_svg_rejected_under_override_even_though_raster_check_ran_first():
    """Confirms the rejection is the Provenance gate, not `sanitize_svg_mark` coincidentally failing on
    this input on its own (it does not -- see the bundled/installed tests above using the same input)."""
    assert sanitize_svg_mark(_TWO_PATH_LOGO) is not None  # the payload itself sanitizes cleanly...
    assert resolve_mark(_TWO_PATH_LOGO, provenance="override") is None  # ...but is still refused here


def test_resolve_mark_hostile_svg_rejected_under_bundled_too():
    """Provenance widens what KIND of Mark is trusted, never how strictly its bytes are checked -- a
    hostile SVG is still hostile even from a bundled/installed source."""
    hostile = '<svg viewBox="0 0 10 10"><script>alert(1)</script></svg>'
    resolved = resolve_mark(hostile, provenance="bundled")
    # No <script> survives; the sanitized svg is what's left (just the empty <svg> shell), never None
    # outright, but nothing dangerous can possibly be in it.
    assert resolved is not None
    assert "script" not in resolved.value.lower()


def test_resolve_mark_remote_url_rejected_under_every_provenance():
    for provenance in PROVENANCES:
        assert resolve_mark("https://evil.example/logo.png", provenance=provenance) is None


def test_resolve_mark_none_payload_returns_none():
    for provenance in PROVENANCES:
        assert resolve_mark(None, provenance=provenance) is None


def test_resolve_mark_unknown_provenance_asserts():
    """`provenance` is a closed, code-defined classification, never attacker input (see PROVENANCES'
    docstring) -- an unrecognised value is a programming error and is asserted, not silently degraded."""
    try:
        resolve_mark(_TINY_PNG, provenance="operator-vibes")
    except AssertionError:
        pass
    else:
        raise AssertionError("expected resolve_mark to assert on an unknown provenance")


# --- resolve_mark: intrinsic size metadata -----------------------------------------------------------

def test_resolve_mark_svg_reports_intrinsic_size_from_viewbox():
    resolved = resolve_mark(_TWO_PATH_LOGO, provenance="bundled")
    assert resolved is not None
    assert (resolved.width, resolved.height) == (64, 24)


def test_resolve_mark_raster_reports_no_intrinsic_size():
    """Deliberately left unknown -- see ResolvedMark's docstring on why decoding raster headers is out
    of scope for this module."""
    resolved = resolve_mark(_TINY_PNG, provenance="bundled")
    assert resolved is not None
    assert (resolved.width, resolved.height) == (None, None)

# --- the CSS-escape decode bypass (fix #1 of bd8d8af) -------------------------------------------------
#
# Adversarial review of #104 flagged that the SSRF/javascript tests all use the LITERAL substrings
# `url(` and `javascript:`, so every one of them would still pass if the `"\\"` entry were dropped from
# _BAD_ATTR_VALUE_SUBSTRINGS -- i.e. the fix had no test that would go red if it were reverted. These
# do. The mechanism: a browser re-parses an attribute value like `fill` with the CSS tokenizer, and
# ident tokenization decodes escapes BEFORE comparing a function name, so `\75 rl(...)` reconstitutes
# as `url(...)` after a substring filter has already approved it.

@pytest.mark.parametrize(
    "value",
    [
        r"\75 rl(https://evil.example/x)",       # \75 == 'u'  -> url(
        r"\000075rl(https://evil.example/x)",    # six-digit form of the same
        r"\75rl(https://evil.example/x)",        # no separating space
        r"u\72 l(https://evil.example/x)",       # escape in the middle
        r"\6a avascript:alert(1)",               # \6a == 'j'  -> javascript:
        r"url\28 https://evil.example/x\29",    # escaped parens instead
    ],
)
def test_a_css_escaped_url_in_an_attribute_value_is_refused(value):
    """Any backslash in an attribute value is refused outright, which is why enumerating decodings is
    not required -- but these prove the guard is actually load-bearing rather than incidental."""
    svg = f'<svg viewBox="0 0 10 10"><path d="M0 0" fill="{value}"/></svg>'
    cleaned = sanitize_svg_mark(svg)
    # Either the whole mark is refused, or it survives with the hostile attribute stripped. What must
    # never happen is the value reaching the output in any form.
    if cleaned is not None:
        assert "\\" not in cleaned
        assert "evil.example" not in cleaned
        assert "url(" not in cleaned.lower()
        assert "javascript" not in cleaned.lower()


def test_the_backslash_guard_is_what_stops_it():
    """Pins the guard itself, so a future "simplification" of _BAD_ATTR_VALUE_SUBSTRINGS that drops the
    backslash entry fails here with a clear reason rather than quietly reopening the bypass."""
    from scribble.reporting.marks import _BAD_ATTR_VALUE_SUBSTRINGS

    assert "\\" in _BAD_ATTR_VALUE_SUBSTRINGS
