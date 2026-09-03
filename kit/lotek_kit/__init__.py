"""``lotek-kit`` — the shared contract library for the lotek platform.

What it is for: lotek core and its extensions both need the same handful of things (the ``attackpath/v1``
document model; one reorder implementation instead of three copies), and the platform's contract says an
extension reaches core only through the injected host contract, never by importing it. Core reaching the
other way is worse still. This package is the neutral ground that lets both sides depend on a contract
instead of on each other.

Two rules keep it neutral, and both are pinned by tests in ``kit/tests/``:

1. **It never imports lotek, any extension, or Flask at module scope.** Flask is an optional extra used
   only by :mod:`lotek_kit.flask_assets`, which imports it inside the function.
2. **It is not an extension and cannot become one.** No ``lotek-extension.toml``, no
   ``lotek.extensions`` entry point, no ``register()``.

**There is deliberately no version-guard helper here.** The obvious one — a ``require("x.y")`` an
extension calls from its ``register()`` — is a security hole rather than a safety net: by the time
``register()`` can raise, ``app.register_blueprint`` has already run and is irreversible, the host's
``_inject_host`` (the only writer of the authorization extras) has NOT yet run, and ``mount_extensions``
catches the exception and carries on. The result would be a mounted surface with no injected
authorization. Version skew is instead made impossible by construction: a consumer resolves the kit
through ``{ path = "../kit", editable = true }``, so it gets the same commit it shipped from, and a
disagreement between core's pin and a consumer's is a hard ``uv lock`` error, not a runtime surprise.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version as _pkg_version

#: What an uninstalled source tree reports. Must equal ``hatch_build.FALLBACK_VERSION``, and cannot
#: simply import it: ``hatch_build`` needs hatchling and runs at BUILD time, long before this module is
#: importable in a consumer's environment. Two copies are structurally unavoidable, so a test asserts
#: they agree rather than leaving it to whoever edits one of them next.
FALLBACK_VERSION = "0.0.0.dev0"

try:
    __version__ = _pkg_version("lotek-kit")
except PackageNotFoundError:  # running from a source tree that was never installed
    __version__ = FALLBACK_VERSION

__all__ = ["FALLBACK_VERSION", "__version__"]
