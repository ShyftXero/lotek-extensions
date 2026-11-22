"""Phase-1 hard rule: sshim imports NOTHING from lotek core (`app.*`).

The rendezvous is a 3rd-party box the operator configures; the whole feature ships with zero core
changes. A single `import app` / `from app ...` anywhere in the package would couple sshim to core and
break standalone import — this sweep fails the moment one appears. (Phase 2's ephemeral-relay seam will
arrive through the injected `extras` hook, never an import — see `deps.host_hook`.)
"""
from __future__ import annotations

import ast
from pathlib import Path

PKG = Path(__file__).resolve().parent.parent / "sshim"


def _app_imports(tree: ast.AST) -> list[str]:
    bad = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            bad += [a.name for a in node.names if a.name == "app" or a.name.startswith("app.")]
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if mod == "app" or mod.startswith("app."):
                bad.append(mod)
    return bad


def test_no_core_app_import_anywhere_in_the_package():
    offenders = {}
    for path in PKG.rglob("*.py"):
        found = _app_imports(ast.parse(path.read_text()))
        if found:
            offenders[path.name] = found
    assert not offenders, f"sshim must not import app.* (Phase 1): {offenders}"


def test_sshim_imports_standalone_without_a_host():
    # If anything reached into core at import time, this would raise.
    import importlib

    import sshim

    importlib.reload(sshim)
    assert callable(sshim.register)
