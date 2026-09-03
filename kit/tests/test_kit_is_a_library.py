"""Guards that keep ``lotek-kit`` a neutral library rather than drifting into an extension.

Every assertion here encodes a decision from map #148 / issue #149 that becomes invisible in the code
once it is made. They are cheap; the failures they prevent are not.
"""

from __future__ import annotations

import ast
import tomllib
from pathlib import Path

KIT_ROOT = Path(__file__).resolve().parent.parent
PKG = KIT_ROOT / "lotek_kit"

#: Importing any of these from the kit would recreate, inside the shared package, exactly the coupling
#: the shared package exists to dissolve.
FORBIDDEN_ROOTS = {"app", "lotek", "bugreport", "cream", "exploiteer", "registrar", "scribble", "vector"}


def _pyproject() -> dict:
    with open(KIT_ROOT / "pyproject.toml", "rb") as fh:
        return tomllib.load(fh)


def _module_scope_imports(module: Path) -> list[str]:
    """Root package names imported at MODULE scope. A lazy import inside a function is deliberately
    invisible here — that is the sanctioned way to reach an optional dependency."""
    tree = ast.parse(module.read_text(encoding="utf-8"))
    roots: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            roots += [alias.name.split(".")[0] for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            roots.append(node.module.split(".")[0])
    return roots


def test_the_kit_has_no_runtime_dependencies():
    """Core takes this as a BASE dependency, so anything here lands in lotek itself and in every
    extension that consumes the kit. An empty list is the whole point; keep it empty."""
    assert _pyproject()["project"]["dependencies"] == []


def test_flask_is_an_optional_extra_not_a_dependency():
    extras = _pyproject()["project"]["optional-dependencies"]
    assert any(spec.startswith("flask") for spec in extras["flask"])


def test_the_version_is_derived_not_typed():
    """A literal ``version`` here would be the only hand-bumped one in the repo — every other
    subproject is frozen at 0.1.0.dev0 with the real version derived from the git build id."""
    project = _pyproject()["project"]
    assert "version" not in project, "version must come from hatch_build.py, not a literal"
    assert project["dynamic"] == ["version"]


def test_the_kit_cannot_be_discovered_as_an_extension():
    """lotek enumerates the ``lotek.extensions`` entry-point group and reads a shipped
    ``lotek-extension.toml``. Neither exists here, and that absence is what makes the kit structurally
    unmountable rather than merely unmounted."""
    entry_points = _pyproject()["project"].get("entry-points", {})
    assert "lotek.extensions" not in entry_points
    assert not list(KIT_ROOT.rglob("lotek-extension.toml"))


def test_no_module_imports_a_host_or_an_extension_at_module_scope():
    offences = [
        f"{module.relative_to(KIT_ROOT)}: {root}"
        for module in sorted(PKG.rglob("*.py"))
        for root in _module_scope_imports(module)
        if root in FORBIDDEN_ROOTS
    ]
    assert offences == [], f"module-scope imports of a host/extension: {offences}"


def test_flask_is_never_imported_at_module_scope():
    """``import lotek_kit.attackpath`` must work with no web framework installed — otherwise the
    optional extra is a lie and core's base dependency drags Flask in through the back door."""
    offences = [
        str(module.relative_to(KIT_ROOT))
        for module in sorted(PKG.rglob("*.py"))
        if "flask" in _module_scope_imports(module)
    ]
    assert offences == [], f"module-scope flask import in: {offences}"


def test_static_assets_ship_as_package_data_without_force_include():
    """CLAUDE.md documents a trap: a force-included file is absent from an editable install and the
    failure is silent. Keeping ``static/`` inside the package directory means there is nothing to
    force-include and nothing to lose."""
    config = _pyproject()["tool"]["hatch"]["build"]
    assert "force-include" not in str(config), "kit assets must not rely on force-include"
    assert config["targets"]["wheel"]["packages"] == ["lotek_kit"]
    assert (PKG / "static").is_dir()
