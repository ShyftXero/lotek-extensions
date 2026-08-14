#!/usr/bin/env python3
"""Compute this monorepo's release tag: ``v<build-id>`` where the build id is the SAME
git-commit-derived, PEP 440-valid string lotek core uses — ``YYYY.M.D.HHMMSS+g<shorthash>`` — from the
HEAD commit's committer date (UTC) plus its abbreviated hash.

Why identical to core: lotek core's ``src/app/_version.py`` derives its own version this way, and its
deploy loop selects the newest release by ``git tag --sort=-creatordate`` over the ``v[0-9]*`` pattern.
Cutting the extensions monorepo's tags in the exact same shape means the two repos' tags are directly
comparable and a lotek build can pin its extensions to a dated release tag rather than a raw SHA.

Dependency-light on purpose: stdlib + ``git`` only. Prints the tag string (``v`` + build id) to stdout.

Format detail: ``%ct`` (committer epoch) avoids a datetime dependency — ``time.gmtime`` gives the UTC
parts; ``%h`` is git's abbreviated hash (same short-hash logic as core). On a repo with no reachable
commit or no ``git``, prints nothing and exits non-zero.
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

# scripts/build_id.py -> the repo root is one parent up.
_REPO_ROOT = Path(__file__).resolve().parents[1]


def git_build_id(root: Path, ref: str = "HEAD") -> str | None:
    """PEP 440 build id from the ``ref`` commit at ``root``: ``YYYY.M.D.HHMMSS+g<shorthash>`` (committer
    date in UTC + abbreviated hash). Returns ``None`` when ``root`` isn't a git checkout, git isn't
    available, or ``ref`` can't be resolved."""
    try:
        out = subprocess.run(
            ["git", "-C", str(root), "show", "-s", "--format=%ct %h", ref],
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


def release_tag(root: Path, ref: str = "HEAD") -> str | None:
    """The release tag: ``v`` + the build id, matching the ``v[0-9]*`` pattern core's deploy loop
    selects. ``None`` when the build id can't be computed."""
    build_id = git_build_id(root, ref)
    return None if build_id is None else f"v{build_id}"


def main(argv: list[str]) -> int:
    # Optional single positional arg: a ref to stamp (defaults to HEAD) — lets a caller compute the tag
    # for origin/main or a specific commit without checking it out.
    ref = argv[1] if len(argv) > 1 else "HEAD"
    tag = release_tag(_REPO_ROOT, ref)
    if tag is None:
        print(f"error: could not compute build id for ref {ref!r} at {_REPO_ROOT}", file=sys.stderr)
        return 1
    print(tag)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
