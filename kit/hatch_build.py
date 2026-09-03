"""Stamp the wheel's version from the monorepo's git build id.

Every other subproject here carries ``version = "0.1.0.dev0"`` and has never moved it: the real version
is derived from git by ``scripts/build_id.py`` and the dated tag CI cuts on merge. A hand-bumped semver
in this package would be the only one in the repo, and would drift the moment anyone forgot it.

So this package has no version literal at all. The format is byte-identical to
``scripts.build_id.git_build_id`` — ``YYYY.M.D.HHMMSS+g<shorthash>``, committer date in UTC — because a
consumer comparing an installed ``lotek-kit`` against a release tag must see the same string.

Stdlib + hatchling only, deliberately: a build backend that needs third-party imports fails in exactly
the environments (fresh CI, a clean container) where a build must not be fragile.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

from hatchling.metadata.plugin.interface import MetadataHookInterface

# What a build outside a git checkout gets — an sdist unpacked from PyPI, say. PEP 440 orders this below
# every real dated version, so a resolver never prefers it to a genuine build.
FALLBACK_VERSION = "0.0.0.dev0"


def _repo_root(start: Path) -> Path | None:
    """The nearest ancestor of ``start`` containing ``.git`` — the monorepo root, not ``kit/``."""
    for candidate in (start, *start.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def _git_build_id(root: Path) -> str | None:
    """``YYYY.M.D.HHMMSS+g<shorthash>`` for HEAD, or None if git can't answer.

    Mirrors ``scripts/build_id.py``. Kept as a copy rather than an import on purpose: a build backend
    cannot rely on the repo's ``scripts/`` directory being present in the sdist it is building from.
    """
    try:
        out = subprocess.run(
            ["git", "-C", str(root), "show", "-s", "--format=%ct %h", "HEAD"],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    parts = out.stdout.split()
    if len(parts) != 2 or not parts[0].isdigit():
        return None
    t = time.gmtime(int(parts[0]))
    hhmmss = t.tm_hour * 10000 + t.tm_min * 100 + t.tm_sec
    return f"{t.tm_year}.{t.tm_mon}.{t.tm_mday}.{hhmmss}+g{parts[1]}"


class BuildIdMetadataHook(MetadataHookInterface):
    def update(self, metadata: dict) -> None:
        root = _repo_root(Path(self.root).resolve())
        build_id = _git_build_id(root) if root is not None else None
        metadata["version"] = build_id or FALLBACK_VERSION
