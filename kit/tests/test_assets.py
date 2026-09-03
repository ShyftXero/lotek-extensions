"""``lotek_kit.assets`` — filesystem access to shipped browser assets.

The blueprint in ``flask_assets`` (next PR in the stack) is a thin wrapper over this, not the other way
round, because a renderer building a self-contained deliverable needs the BYTES to inline and cannot get
them from a URL.
"""

from __future__ import annotations

import pytest

from lotek_kit.assets import AssetNotFound, asset_bytes, asset_text


@pytest.mark.parametrize(
    "name",
    [
        "",
        ".",
        "..",
        "../pyproject.toml",
        "../../scripts/build_id.py",
        "sub/dir.js",
        "back\\slash.js",
        ".hidden",
    ],
)
def test_a_name_that_could_escape_the_package_is_refused(name):
    """Refused outright rather than normalized: there is no legitimate caller for a nested or relative
    asset name, so accepting one and cleaning it up would only create somewhere for a bug to hide."""
    with pytest.raises(AssetNotFound):
        asset_bytes(name)


def test_an_unknown_asset_raises_a_distinguishable_error():
    """``AssetNotFound`` rather than a bare OSError, so a caller can tell 'you asked for the wrong
    thing' apart from 'the disk is broken'."""
    with pytest.raises(AssetNotFound):
        asset_text("no-such-file.js")


def test_asset_not_found_is_catchable_as_lookup_error():
    assert issubclass(AssetNotFound, LookupError)


def test_a_name_containing_a_null_byte_raises_the_documented_error():
    """A NUL in a path makes the stdlib raise ValueError, not the module's own error. A caller
    catching AssetNotFound would otherwise get something it never agreed to handle."""
    with pytest.raises(AssetNotFound):
        asset_bytes("x\x00.js")


def test_the_static_package_actually_resolves():
    """``static/`` has no ``__init__.py``, so it is a namespace package and ``files()`` returns a
    MultiplexedPath rather than a plain path. Every other test here asserts a FAILURE, so without
    this one a totally unresolvable asset directory would ship green — the reachable assets, and the
    success path over them, arrive with the reorder assets in the stacked branch.

    Resolved through the module's own ``_STATIC`` constant, deliberately: an earlier version of this
    test hardcoded the package name, which meant breaking ``_STATIC`` left the test green. A guard
    that cannot see the break it exists for is worse than no guard, because it reads as coverage.
    """
    from importlib.resources import files

    from lotek_kit.assets import _STATIC

    assert files(_STATIC).is_dir()


@pytest.mark.parametrize("name", ["reorder.js", "reorder.css"])
def test_a_shipped_asset_reads_back_as_bytes_and_as_text(name):
    """The bytes path is the one a self-contained deliverable needs: ``vector/vector/render.py``
    inlines its viewer off disk so the exported HTML makes no external request, and a URL cannot serve
    that."""
    assert asset_bytes(name)
    assert asset_text(name).strip()
    assert asset_bytes(name).decode("utf-8") == asset_text(name)
