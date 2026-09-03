"""Filesystem access to the kit's browser assets, with no web framework involved.

Two consumers want different things from the same files. A mounted Flask app wants them on a URL (see
:mod:`lotek_kit.flask_assets`). A renderer building a self-contained deliverable wants the *bytes*, to
inline — which is what ``vector/vector/render.py`` does today, reading its viewer JS off disk so the
exported HTML makes no external request. Serving through Flask cannot satisfy the second, so the
filesystem accessor is the primitive and the blueprint is a thin wrapper over it.

Stdlib only: :mod:`importlib.resources`, so this keeps working from inside a wheel or a zipimport where
``__file__``-relative paths do not.
"""

from __future__ import annotations

from importlib.resources import files

_STATIC = "lotek_kit.static"


class AssetNotFound(LookupError):
    """Raised for an asset name that is not shipped. Distinguishable from a genuine IO error."""


def _resource(name: str):
    # A name with a path separator would escape the package directory via the traversable API, so it is
    # refused outright rather than normalized — there is no legitimate caller for a nested asset.
    if not name or "/" in name or "\\" in name or name.startswith("."):
        raise AssetNotFound(f"not a shippable asset name: {name!r}")
    resource = files(_STATIC).joinpath(name)
    if not resource.is_file():
        raise AssetNotFound(f"no such asset: {name!r}")
    return resource


def asset_bytes(name: str) -> bytes:
    """The raw bytes of a shipped asset, for inlining into a self-contained document."""
    return _resource(name).read_bytes()


def asset_text(name: str, encoding: str = "utf-8") -> str:
    """A shipped asset decoded as text."""
    return _resource(name).read_text(encoding=encoding)
