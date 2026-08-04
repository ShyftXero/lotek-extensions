"""Version reporting.

Mirrors Lotek's git-build-id scheme when a git checkout is available; falls back to the packaged
static version otherwise. Kept dependency-free so it is safe to import anywhere.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

_STATIC = "0.1.0.dev0"


def git_build_id() -> str | None:
    """Return ``YYYY.M.D.HHMMSS+g<shorthash>`` from the HEAD commit, or None if unavailable."""
    repo = Path(__file__).resolve().parent.parent
    try:
        out = subprocess.run(
            ["git", "-C", str(repo), "show", "-s", "--format=%cd|%h", "--date=format:%Y.%-m.%-d.%H%M%S"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0 or "|" not in out.stdout:
        return None
    date, short = out.stdout.strip().split("|", 1)
    return f"{date}+g{short}"


__version__ = git_build_id() or _STATIC
