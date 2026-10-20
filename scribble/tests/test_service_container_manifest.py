"""Guard: scribble declares its Gotenberg sidecar in the manifest AND ships the build context.

lotek #1265 makes the host BUILD + ensure an extension's declared service containers at deploy prestart
(from the Dockerfile shipped in the wheel). That only works if (a) the manifest declares the container
and (b) `scribble/gotenberg/` actually ships inside the package. Before #1265 the Gotenberg image was
never built on prod and PDF export silently died — these two facts are what make a fresh deploy render a
PDF, so they are pinned here. A dropped `[[service_containers]]` block or a dropped wheel force-include
regresses exactly that, invisibly, so it fails this test instead.
"""
from __future__ import annotations

import tomllib
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]  # the `scribble/` project dir
_MANIFEST = _ROOT / "lotek-extension.toml"
_PYPROJECT = _ROOT / "pyproject.toml"


def _gotenberg_entry() -> dict:
    data = tomllib.loads(_MANIFEST.read_text(encoding="utf-8"))
    entries = data.get("service_containers") or []
    matches = [e for e in entries if e.get("name") == "gotenberg"]
    assert matches, "manifest must declare a [[service_containers]] entry named 'gotenberg'"
    return matches[0]


def test_manifest_declares_gotenberg_service_container():
    entry = _gotenberg_entry()
    from scribble.reporting import exporters
    # image must match scribble's own DEFAULT image so the prestart build and the lazy hook target one
    # image. Compared against the LITERAL default, env-independent: exporters.GOTENBERG_IMAGE is resolved
    # from SCRIBBLE_GOTENBERG_IMAGE at import, and that var is a documented operator override (see the
    # Dockerfile / exporters.py) — comparing against the live value false-REDs on a box that has it set.
    assert entry["image"] == "lotek-gotenberg:latest"  # == exporters.GOTENBERG_IMAGE default
    assert int(entry["port"]) == exporters.GOTENBERG_PORT  # a plain literal, no env coupling
    # build_context is package-relative; it must resolve to a real Dockerfile dir in the source tree.
    ctx = _ROOT / entry["build_context"]
    assert (ctx / "Dockerfile").is_file(), f"build_context {entry['build_context']!r} must hold a Dockerfile"


def test_wheel_force_includes_the_build_context():
    """The build context resolves out of the INSTALLED wheel via importlib.resources — which requires a
    force-include mapping it under the package dir. A missing mapping = prod can't build the image."""
    data = tomllib.loads(_PYPROJECT.read_text(encoding="utf-8"))
    force = data["tool"]["hatch"]["build"]["targets"]["wheel"]["force-include"]
    entry = _gotenberg_entry()
    # the build_context dir must be force-included under scribble/<build_context> in the wheel.
    want_dest = f"scribble/{entry['build_context']}"
    assert want_dest in force.values(), (
        f"pyproject wheel force-include must ship {want_dest!r} so the Dockerfile is in the installed wheel"
    )
