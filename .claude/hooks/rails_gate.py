#!/usr/bin/env python3
"""lotek-extensions commit gate — a focused PreToolUse hook enforcing the core rails.

Adopts lotek's flow without its framework-specific machinery. Three rules on `git` Bash commands:

  1. explicit-staging — block `git add -A` / `git add .` / `commit -a` (bulk stage sweeps in unrelated
     work and lands it on the wrong branch — stage explicit paths).
  2. no-commit-on-main — block a `git commit` while HEAD is `main` (work on a feature branch; PR in).
  3. ruff-clean         — a `git commit` requires `ruff check` clean on the staged Python.

Escape hatch: prefix `RAILS_OVERRIDE=1` to bypass once (logged). Fails OPEN on any infra error — a broken
gate never blocks legitimate work — and appends every block/override/fail-open to
`<git-dir>/claude-rails-audit.jsonl` so a skipped gate is never invisible.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path


def _repo_root() -> Path:
    try:
        out = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, timeout=5)
        return Path(out.stdout.strip()) if out.returncode == 0 and out.stdout.strip() else Path.cwd()
    except (OSError, subprocess.SubprocessError):
        return Path.cwd()


def _audit(entry: dict) -> None:
    try:
        gitdir = _repo_root() / ".git"
        (gitdir / "claude-rails-audit.jsonl").open("a", encoding="utf-8").write(json.dumps(entry) + "\n")
    except OSError:
        pass


def _deny(reason: str, command: str) -> None:
    _audit({"ts": time.time(), "action": "deny", "reason": reason, "command": command[:400]})
    print(f"rails: {reason}", file=sys.stderr)
    sys.exit(2)  # PreToolUse: non-zero blocks the tool call; stderr is shown to the model


def _staged_python(root: Path) -> list[str]:
    out = subprocess.run(["git", "diff", "--cached", "--name-only", "--diff-filter=ACM"],
                         capture_output=True, text=True, timeout=10)
    return [f for f in out.stdout.splitlines() if f.endswith(".py") and (root / f).exists()]


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except Exception:  # noqa: BLE001 - no parseable input: allow (fail open)
        sys.exit(0)
    if payload.get("tool_name") != "Bash":
        sys.exit(0)
    command = (payload.get("tool_input") or {}).get("command", "") or ""
    if "RAILS_OVERRIDE=1" in command:
        _audit({"ts": time.time(), "action": "override", "command": command[:400]})
        sys.exit(0)
    if not re.search(r"\bgit\b", command):
        sys.exit(0)

    # 1. explicit-staging
    if re.search(r"\bgit\s+add\s+(-A\b|--all\b|\.(\s|$))", command) or re.search(r"\bgit\s+commit\b[^|&;]*\s-\w*a", command):
        _deny("bulk staging is blocked — stage explicit paths (`git add <path> …`). "
              "Intentional? prefix RAILS_OVERRIDE=1.", command)

    if not re.search(r"\bgit\s+commit\b", command):
        sys.exit(0)

    root = _repo_root()
    # 2. no-commit-on-main
    try:
        branch = subprocess.run(["git", "-C", str(root), "rev-parse", "--abbrev-ref", "HEAD"],
                                capture_output=True, text=True, timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        branch = ""
    if branch == "main":
        _deny("refusing to commit on `main` — cut a feature branch and open a PR. "
              "Intentional (rare)? prefix RAILS_OVERRIDE=1.", command)

    # 3. ruff-clean on staged python (fail open if ruff is unavailable)
    ruff = shutil.which("ruff") or shutil.which("uvx")
    if ruff:
        files = _staged_python(root)
        if files:
            cmd = (["uvx", "ruff", "check", *files] if ruff.endswith("uvx") else ["ruff", "check", *files])
            try:
                res = subprocess.run(cmd, cwd=str(root), capture_output=True, text=True, timeout=120)
            except (OSError, subprocess.SubprocessError) as exc:
                _audit({"ts": time.time(), "action": "fail-open", "reason": f"ruff infra: {exc}"})
                sys.exit(0)
            if res.returncode != 0:
                _deny("ruff is not clean on the staged Python — fix it (or RAILS_OVERRIDE=1):\n"
                      + (res.stdout or res.stderr)[-1500:], command)
    sys.exit(0)


if __name__ == "__main__":
    main()
