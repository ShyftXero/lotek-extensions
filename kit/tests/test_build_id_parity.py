"""The kit's version hook and the repo's release script must agree, forever.

``kit/hatch_build.py`` deliberately COPIES ``scripts/build_id.py``'s format rather than importing it: a
build backend cannot rely on the repo's ``scripts/`` directory being present in the sdist it is building
from. A copy is a hand-synced pair, and a hand-synced pair with no guard is drift waiting to happen — so
this is the guard. If the release-tag format ever changes, this fails, rather than shipping a wheel whose
version silently no longer matches the tag it was cut from.
"""

from __future__ import annotations

import ast
import importlib.util
import sys
from pathlib import Path

import pytest

KIT_ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = KIT_ROOT.parent

#: Only the standard library and the build backend itself. A build backend that reaches for a
#: third-party import fails in exactly the clean environments where a build must not be fragile.
ALLOWED_BUILD_IMPORTS = {"__future__", "subprocess", "time", "pathlib", "hatchling"}


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


requires_checkout = pytest.mark.skipif(
    not (REPO_ROOT / ".git").exists(), reason="not a git checkout — nothing to compare against"
)


@requires_checkout
def test_the_hook_reproduces_the_release_script_byte_for_byte():
    hook = _load(KIT_ROOT / "hatch_build.py", "_kit_hatch_build")
    build_id = _load(REPO_ROOT / "scripts" / "build_id.py", "_repo_build_id")

    ours = hook._git_build_id(REPO_ROOT)
    theirs = build_id.git_build_id(REPO_ROOT)
    assert ours is not None, "HEAD should be resolvable inside a checkout"
    assert ours == theirs, f"build-id format drifted: kit={ours!r} scripts={theirs!r}"


@requires_checkout
def test_the_release_tag_is_the_build_id_with_a_v():
    """What the hook stamps and what ``release-tag.yml`` cuts differ by exactly one character. A
    consumer comparing an installed version against a release tag depends on that."""
    build_id = _load(REPO_ROOT / "scripts" / "build_id.py", "_repo_build_id_tag")
    assert build_id.release_tag(REPO_ROOT) == f"v{build_id.git_build_id(REPO_ROOT)}"


def test_the_hook_falls_back_rather_than_exploding_outside_a_checkout(tmp_path):
    """Building from an unpacked sdist has no ``.git``. That must yield a low, well-ordered version,
    not a traceback inside the build backend."""
    hook = _load(KIT_ROOT / "hatch_build.py", "_kit_hatch_build_fallback")
    assert hook._repo_root(tmp_path) is None
    assert hook.FALLBACK_VERSION == "0.0.0.dev0"


def test_the_hook_needs_no_third_party_imports():
    tree = ast.parse((KIT_ROOT / "hatch_build.py").read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert imported <= ALLOWED_BUILD_IMPORTS, f"unexpected build-time imports: {imported - ALLOWED_BUILD_IMPORTS}"
