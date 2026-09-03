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
