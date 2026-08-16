#!/usr/bin/env python3
"""Shared utilities for the lotek-extensions "rails" PreToolUse gate.

Ported from lotek's `.claude/hooks/_rails.py` (git@github.com:ShyftXero/lotek, same author's
design) — this module is stdlib-only and repo-agnostic: quoting/segment parsing, git plumbing,
and JSONL audit logging don't know or care which repo they're running in. `rails_gate.py` is the
part that differs (this repo has no INVARIANTS.md / branch-owner / merge-gate machinery — see its
module docstring for what was deliberately NOT ported and why).

Two non-negotiable invariants every gate built on this module must keep:

1. **Fail-open.** Any ambiguity — unparseable hook input, not a git repo, detached
   HEAD, a checker that won't launch, an IO error — must ALLOW the command. A guard
   that blocks legitimate work on its own infra failure is worse than no guard.
2. **No silent skips.** Every override and every fail-open is appended to the audit
   log (`audit()`), so a gate that didn't actually gate is visible after the fact.
   This is the "verify + log bypasses" posture: we stay lenient, but never quiet.

Override convention: the single canonical token is `RAILS_OVERRIDE=1`, recognized only as a
LEADING env-assignment (so a token buried inside a `-m` commit message or a quoted value can't
satisfy a gate). `CHECKS_DONE=1` is kept as a legacy alias for the clean-checks gate, matching the
original (pre-port) ext gate's escape hatch.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from typing import NoReturn

CANONICAL_OVERRIDE = "RAILS_OVERRIDE=1"
# {token: gate-it-historically-belonged-to} — honored but logged as deprecated.
LEGACY_OVERRIDES = {"CHECKS_DONE=1": "clean-checks"}

AUDIT_BASENAME = "claude-rails-audit.jsonl"

# ----------------------------------------------------------------------------- io

def read_input() -> dict | None:
    """Parse the PreToolUse hook JSON from stdin. None on any error (→ fail-open)."""
    try:
        data = json.load(sys.stdin)
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def bash_command(data: dict | None) -> str | None:
    """The Bash command string, or None if this isn't a non-empty Bash invocation."""
    if not data or data.get("tool_name") != "Bash":
        return None
    cmd = (data.get("tool_input") or {}).get("command")
    return cmd if isinstance(cmd, str) and cmd else None


def emit_allow() -> NoReturn:
    """No output + exit 0 = passthrough; the tool proceeds normally."""
    sys.exit(0)


def emit_context(message: str) -> NoReturn:
    """Allow, but attach non-blocking context the agent will see (soft advisories)."""
    print(json.dumps({
        "hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": message}
    }))
    sys.exit(0)


def emit_deny(reason: str) -> NoReturn:
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }))
    sys.exit(0)

# -------------------------------------------------------------------------- shell

# Top-level segment boundaries (best-effort; quoting-blind by design — we only use
# this to find a command word's leading env-assignments and to bound a subcommand).
_SEG_SPLIT = re.compile(r"[;|&\n]+")
_QUOTED = re.compile(r"'[^']*'|\"[^\"]*\"")


def segments(cmd: str) -> list[str]:
    return [s.strip() for s in _SEG_SPLIT.split(cmd) if s.strip()]


_HEREDOC = re.compile(
    r"<<-?[ \t]*(?P<q>['\"]?)(?P<tag>[A-Za-z_][A-Za-z0-9_]*)(?P=q)"
    r"(?P<body>.*?)(?:^[ \t]*(?P=tag)[ \t]*$|\Z)",
    re.DOTALL | re.MULTILINE,
)


def strip_heredocs(cmd: str) -> str:
    """Blank out heredoc BODIES, so *document text* can't masquerade as a command.

    Same defect class ``strip_quoted`` already handles for quoted spans: writing a doc that merely
    mentions a gated phrase (``cat > plans/x.md <<'EOF' … gh pr create … EOF``) must not make the
    pre-PR gate fire on an unrelated ``git add``. A gate that denies you for *describing* it teaches
    an operator to reflex-prefix ``RAILS_OVERRIDE=1``.

    Length and newlines are preserved so offsets stay comparable; an unterminated heredoc blanks to
    end-of-string, which is the fail-safe direction (less text to match, never more).
    """
    def _blank(m: re.Match[str]) -> str:
        head_len = m.start("body") - m.start(0)
        whole = m.group(0)
        return whole[:head_len] + re.sub(r"[^\n]", " ", whole[head_len:])

    return _HEREDOC.sub(_blank, cmd)


def strip_quoted(cmd: str) -> str:
    """Blank out heredoc bodies and quoted spans so a commit MESSAGE or a document body can't
    masquerade as a subcommand (e.g. `git commit -m "then git merge"` must not trip a merge gate,
    and neither must a heredoc that documents one). Heredocs first: a body may contain quote
    characters that would otherwise mis-pair with the surrounding command's quoting."""
    return _QUOTED.sub(" ", strip_heredocs(cmd))


def runs_git_subcommand(cmd: str, *names: str) -> bool:
    """True if a (quote-stripped) segment invokes `git <name>` as a standalone
    subcommand (not `commit-graph` etc.)."""
    alt = "|".join(re.escape(n) for n in names)
    pat = re.compile(rf"\bgit\b[^\n;|&]*?\b({alt})\b(?![-\w])")
    return bool(pat.search(strip_quoted(cmd)))


def _leading_assignments(seg: str) -> str:
    """The run of `VAR=value` env-assignments before the command word in a segment."""
    m = re.match(r"((?:[A-Za-z_]\w*=\S+\s+)*)", seg.lstrip())
    return m.group(1) if m else ""


def has_token_assignment(cmd: str, token: str) -> bool:
    """True if `token` (e.g. `RAILS_OVERRIDE=1`) appears as a LEADING env-assignment
    in some segment — not as a substring of a message, path, or quoted value."""
    name = token.split("=", 1)[0]
    pat = re.compile(rf"\b{re.escape(name)}=1\b")
    return any(pat.search(_leading_assignments(seg)) for seg in segments(cmd))


def override(cmd: str) -> str | None:
    """The override token in effect (canonical or a legacy alias), or None."""
    if has_token_assignment(cmd, CANONICAL_OVERRIDE):
        return CANONICAL_OVERRIDE
    for tok in LEGACY_OVERRIDES:
        if has_token_assignment(cmd, tok):
            return tok
    return None

# ---------------------------------------------------------------------------- git

def git(cwd: str, *args: str, timeout: int = 10) -> str | None:
    """Run a git command; return stripped stdout, or None on any failure."""
    try:
        out = subprocess.run(["git", "-C", cwd, *args],
                             capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() if out.returncode == 0 else None


def current_branch(cwd: str) -> str | None:
    b = git(cwd, "rev-parse", "--abbrev-ref", "HEAD")
    return b if b and b != "HEAD" else None


def git_dir(cwd: str) -> str | None:
    return git(cwd, "rev-parse", "--absolute-git-dir")

# -------------------------------------------------------------------------- audit

def audit(cwd: str, *, gate: str, action: str, session_id: str | None = None,
          branch: str | None = None, cmd: str | None = None, detail: str | None = None) -> None:
    """Append one JSONL audit record. Best-effort; never raises.

    `action` is one of: allow | deny | override | fail-open | warn | skip. Overrides and
    fail-opens MUST be logged here so a skipped gate is never invisible.
    """
    gd = git_dir(cwd)
    if not gd:
        return
    rec = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "gate": gate,
        "action": action,
        "session": session_id,
        "branch": branch,
        "cmd": (cmd or "")[:300],
    }
    if detail:
        rec["detail"] = detail
    try:
        with open(os.path.join(gd, AUDIT_BASENAME), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec) + "\n")
    except OSError:
        pass
