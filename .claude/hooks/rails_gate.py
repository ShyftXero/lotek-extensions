#!/usr/bin/env python3
"""The lotek-extensions "rails" PreToolUse gate.

Ports lotek core's commit/PR-gate machinery (git@github.com:ShyftXero/lotek,
`.claude/hooks/rails_gate.py`, same author's design) onto this repo — a monorepo of four
independent `uv` subprojects (`cream/`, `registrar/`, `scribble/`, `vector/`), each with its own
`pyproject.toml` + `tests/`, and no root-level Python project.

Gates (in evaluation order):
  1. explicit-staging  — deny `git add -A/.`/`git commit -a`; stage explicit paths instead.
  2. protected-branch   — refuse an agent commit directly on `main`.
  3. pre-pr-review      — `gh pr create` needs a local test pass + a security review + an
                          adversarial review recorded for HEAD (see markers below).
  4. invariant-pointer   — non-blocking: `gh pr create` gets reminded that the canonical
                          security-invariant contract (INVARIANTS.md) lives in lotek CORE, not
                          here (see "Invariant divergence" below).
  5. clean-checks       — ruff + pyrefly must be clean on staged Python before a commit (runs
                          last; it's the slow one). pyrefly runs PER SUBPROJECT, from that
                          subproject's own directory, since each has its own `uv` environment.

Override (normalized): `RAILS_OVERRIDE=1 <cmd>` bypasses every applicable gate; the legacy
`CHECKS_DONE=1` token still bypasses clean-checks specifically. Every override, deny, fail-open,
and soft-warn is appended to `<git-dir>/claude-rails-audit.jsonl`. Fail-open everywhere: infra
failure never blocks legitimate work.

Also a tiny CLI — marker recorders, every one (except `--ack-invariants`) gating `gh pr create`:
  * `--ack-review`      a security-review marker for HEAD (after /security-review on the full
                        `git diff main...HEAD`), or `--ack-review --staged` for the staged tree.
  * `--ack-tests`       a local-test-pass marker for HEAD — the human asserts the relevant
                        subproject suite(s) are green (`cd <ext> && uv run --extra dev pytest -q`),
                        same trust model as lotek's `--ack-tests`.
  * `--ack-adversarial` an adversarial-review marker for HEAD (after /adversarial-reviewer), or
                        `--ack-adversarial --staged` for the staged tree.
  * `--ack-transcripts` a red-then-green-transcript marker for HEAD — required only when the
                        branch touches some subproject's `tests/` dir.
  * `--ack-invariants`  DOES NOT GATE ANYTHING. See "Invariant divergence" below — this repo has
                        no local `pytest -m invariant` contract, so this CLI only DETECTS whether
                        any `@pytest.mark.invariant` usage exists and logs an honest SKIP, never a
                        fake green, never a required marker.
These markers gate `gh pr create` for the same reason they do in lotek: there is no CI here that
can be trusted as a merge gate, so local verification before the PR is the only real one.

## Invariant divergence (intentional — read before "fixing" this)

lotek core has a deterministic `INVARIANTS.md` + `pytest -m invariant` contract (~150 tagged
tests) and a NON-OPT-IN `--ack-invariants` gate that RUNS that suite and only records a marker if
it's green. This repo has **no equivalent registry**: `INV-…` ids that appear in this repo's code
(INV-EXT-*, INV-TENANCY-05/06, INV-INTEGRITY-03, INV-AUDIT-03/04, INV-SECRET-05, …) are inline
references INTO lotek core's `INVARIANTS.md` — the canonical text and the enforcing test suite
both live there, not here. Porting `--ack-invariants` as a REQUIRED `gh pr create` gate in this
repo would therefore fake a green check for a contract this repo cannot itself run or verify.

So: `--ack-invariants` is kept as a CLI (for muscle-memory parity with lotek), but it is NOT in
`_PRE_PR_REQUIREMENTS` and never blocks anything. It scans the four subprojects' `tests/` dirs for
`@pytest.mark.invariant` usage (there is none today) and logs a SKIP-with-reason to the audit
trail — an honest "this doesn't apply here yet", never a fabricated pass. See `ack_invariants()`.
The `invariant-pointer` gate (non-blocking, on `gh pr create`) is the compensating control: it
reminds the author that lotek's INVARIANTS.md is the contract to consult before merging a change
that touches a core-reference id, tenancy scoping, audit emission, or a confirm-tier verb.

## What was NOT ported (deliberately, out of scope for this pass)

lotek core also has `branch-owner` (shared-worktree session collision guard), `merge-gate` (local
`git merge` into main), `push-identity` (bot-token push enforcement), `regression-test` and
`ci-required-checks` (soft advisories). None of those are ported here — this pass only imposes the
same commit/PR-gate BAR (lint/types clean + reviewed + tested before a PR), not lotek's full
concurrent-session machinery. Add them later if this repo grows the same multi-session pressure
lotek did; don't assume their absence is an oversight.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from typing import Callable, NamedTuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _rails as R  # noqa: E402

REVIEW_MARKER_BASENAME = "claude-security-review.json"
LOCAL_TESTS_MARKER_BASENAME = "claude-local-tests.json"
ADVERSARIAL_MARKER_BASENAME = "claude-adversarial-review.json"
TRANSCRIPTS_MARKER_BASENAME = "claude-guard-transcripts.json"

# The independent uv subprojects. A staged path's first component identifies which one owns it (or
# None for a repo-level path like CLAUDE.md, scripts/, .github/, plans/).
#
# `kit` is NOT an extension — it is the shared contract library (see kit/README.md) — but it is a
# subproject in every sense this gate cares about: its own pyproject.toml, its own venv and lock, its
# own dev extra. So it belongs here, or `uv run --extra dev pyrefly check` would never run against it.
#
# KNOWN GAP, pre-existing and deliberately not widened here: `bugreport` and `exploiteer` are also
# subprojects on disk and are absent from this tuple, so staged Python under them skips the per-project
# pyrefly pass. Adding them changes the gate's behaviour for code this branch does not touch.
_SUBPROJECTS = ("cream", "kit", "registrar", "scribble", "vector")

# lotek core's canonical invariant contract. Checked locally (best-effort, never required) so the
# reminder can point at a real path when one exists; falls back to the public URL. This repo must
# run standalone without lotek core present, so absence here is NEVER an error — see
# `_lotek_invariants_hint`.
_LOTEK_INVARIANTS_URL = "https://github.com/ShyftXero/lotek/blob/main/INVARIANTS.md"
_LOTEK_INVARIANTS_LOCAL_CANDIDATES = (
    os.path.expanduser("~/Dropbox/code/lotek/INVARIANTS.md"),
    os.path.expanduser("~/code/lotek/INVARIANTS.md"),
)

Verdict = tuple[str, str] | None  # ("deny"|"context", message) or None to pass.


def _resolve_dir(raw: str, base: str) -> str:
    """A shell path token (quotes stripped, ~ expanded) resolved against ``base``; ``base`` if it isn't
    an existing directory (never invent a cwd the shell wouldn't actually be in)."""
    p = os.path.expanduser(raw.strip("'\""))
    if not os.path.isabs(p):
        p = os.path.normpath(os.path.join(base, p))
    return p if os.path.isdir(p) else base


def _effective_cwd(cmd: str, cwd: str) -> str:
    """The directory the gated git command will actually run in. The harness reports the ambient cwd,
    but a command can move first — the common one is committing in a linked worktree
    (`cd ../ext-topic && git commit`) or against one (`git -C ../ext-topic commit`). Marker files and
    `git write-tree` are per-worktree, so evaluating the gate in the ambient cwd would miss a valid
    `--ack-review` recorded in the worktree and false-deny the commit.

    Follow what the shell does: apply every leading `cd <path> &&`/`;` segment in order, then any
    `git -C <path>` on the git invocation (relative to that). Ported verbatim from lotek's
    `_rails.py` sibling — this logic is repo-agnostic."""
    working = cwd
    rest = cmd
    seg = re.compile(r"\s*cd\s+(?P<p>'[^']*'|\"[^\"]*\"|[^\s;&|]+)[ \t]*(?:&&|;|\n)\s*")
    while (m := seg.match(rest)) is not None:
        working = _resolve_dir(m.group("p"), working)
        rest = rest[m.end():]
    gm = re.search(r"\bgit\s+-C\s+(?P<p>'[^']*'|\"[^\"]*\"|[^\s;&|]+)", rest)
    if gm:
        working = _resolve_dir(gm.group("p"), working)
    return working


class Ctx:
    """Everything the gates need, computed once per invocation."""

    def __init__(self, cmd: str, cwd: str, session_id: str | None) -> None:
        self.cmd = cmd
        self.cwd = _effective_cwd(cmd, cwd)
        self.session_id = session_id
        self.contexts: list[str] = []
        self._branch: str | None | object = _UNSET

    @property
    def branch(self) -> str | None:
        if self._branch is _UNSET:
            self._branch = R.current_branch(self.cwd)
        return self._branch  # type: ignore[return-value]

    def is_commit(self) -> bool:
        return R.runs_git_subcommand(self.cmd, "commit") and "--dry-run" not in self.cmd

    def log(self, gate: str, action: str, detail: str | None = None) -> None:
        R.audit(self.cwd, gate=gate, action=action, session_id=self.session_id,
                branch=self.branch, cmd=self.cmd, detail=detail)


_UNSET = object()

# --------------------------------------------------------------------------- gates

def _g_explicit_staging(ctx: Ctx) -> Verdict:
    """Block the bulk-staging forms that have repeatedly co-mingled unrelated work
    (`git add -A/.`, `git commit -a`). Stage explicit paths instead."""
    s = R.strip_quoted(ctx.cmd)
    bulk_add = re.search(r"\bgit\s+add\b[^|;&]*?(?:\s(?:-A|--all)\b|\s[.*](?:\s|$))", s)
    bulk_commit = re.search(r"\bgit\s+commit\b[^|;&]*?(?:\s--all\b|\s-(?=[A-Za-z]*a)[A-Za-z]+)", s)
    if not (bulk_add or bulk_commit):
        return None
    what = "git add -A/." if bulk_add else "git commit -a"
    return ("deny",
            f"Bulk staging (`{what}`) is blocked in this repo.\n\n"
            "It has repeatedly swept in unrelated edits from another extension's subdir and landed "
            "them on the wrong branch. Stage explicit paths instead:\n\n"
            "    git add <path> <path> && git diff --cached --name-only\n\n"
            "then commit. Intentional bulk stage (rare): prefix RAILS_OVERRIDE=1.")


_PROTECTED_BRANCHES = {"main"}


def _g_protected_branch(ctx: Ctx) -> Verdict:
    """An agent works on a feature branch, never directly on `main`. GitHub Flow: short-lived
    branch → PR → squash-merge → auto-delete."""
    if not ctx.is_commit():
        return None
    if ctx.branch not in _PROTECTED_BRANCHES:
        return None
    return ("deny",
            f"Refusing to commit directly on protected branch '{ctx.branch}'. Move the work to a "
            "branch:\n\n    git switch -c fix/<slug>    # your staged changes come with you\n\n"
            "then commit. Intentional commit on this branch (rare): prefix RAILS_OVERRIDE=1.")


def _runs_gh_pr_create(cmd: str) -> bool:
    """True if a (quote-stripped) segment opens a PR via `gh pr create` — not view/list/merge/comment."""
    return bool(re.search(r"\bgh\s+pr\s+create\b", R.strip_quoted(cmd)))


# --------------------------------------------------------------------- push identity (ported from lotek)
# A `git push` to GitHub from an agent session must authenticate as the bot, never over SSH / the human's
# credentials — else the HUMAN becomes the "last pusher" and require_last_push_approval bars them from
# approving. Ported from lotek core (2026-08-22) so this repo has parity; the primary lever is the
# transparent bot-auth wired into agent WORKTREES by lotek's scripts/install-bot-push-auth.sh, and this
# gate is the backstop for a plain push from a PRIMARY checkout.

_PUSH_OPT_TAKES_VALUE = {"-o", "--push-option", "--repo", "--receive-pack", "--exec"}


def _push_args(cmd: str) -> list[str] | None:
    """The argument tokens following `git [-C <dir>] push` on its own segment, or None if not a push."""
    m = re.search(r"\bgit\b(?:\s+-C\s+\S+)?\s+push\b(?P<rest>[^\n;|&]*)", R.strip_quoted(cmd))
    return m.group("rest").split() if m else None


def _push_remote(cmd: str) -> str:
    """The remote a `git push` targets: first positional (skipping flags + value-taking flag values), or
    `origin` when none is given. Token may be a remote NAME or a bare URL."""
    skip = False
    for tok in _push_args(cmd) or []:
        if skip:
            skip = False
            continue
        if tok in _PUSH_OPT_TAKES_VALUE:
            skip = True
            continue
        if tok.startswith("-"):
            continue
        return tok
    return "origin"


def _bot_auth_active(cwd: str) -> bool:
    """True when this checkout is wired for TRANSPARENT bot-identity pushes: a
    `url.https://github.com/.insteadOf git@github.com:` rewrite PLUS a github.com credential helper that
    mints the bot's App token (`x-access-token` / gh-app-token.py). That is what lotek's
    scripts/install-bot-push-auth.sh installs (~/.config/git/lotek-bot.inc), scoped by an
    `includeIf gitdir:…/lotek*/.git/worktrees/` to AGENT worktrees only. There a plain `git push` to an
    SSH-form remote is rewritten to an `x-access-token` HTTPS push at transport time — so the BOT is the
    pusher even though `git remote get-url` still shows `git@github.com:`, which is why the URL check
    below cannot see it and this predicate is needed to avoid a false deny on the sanctioned path."""
    insteadof = R.git(cwd, "config", "--get", "url.https://github.com/.insteadOf")
    if insteadof != "git@github.com:":
        return False
    helpers = R.git(cwd, "config", "--get-all", "credential.https://github.com.helper") or ""
    return "x-access-token" in helpers or "gh-app-token" in helpers


def _g_push_identity(ctx: Ctx) -> Verdict:
    """Deny a `git push` to GitHub that would authenticate as the human (SSH / stored creds). Two
    sanctioned paths make the BOT the pusher: an AGENT WORKTREE wired by lotek-bot.inc (a plain push is
    transparently bot-authed — see `_bot_auth_active`), or `scripts/agent-push.sh` (ephemeral token
    remote). Primary checkouts are not wired, so a plain push there is denied."""
    args = _push_args(ctx.cmd)
    if args is None:
        return None
    if "--dry-run" in args or "-n" in args:
        return None  # no ref change ⇒ no pusher change
    remote = _push_remote(ctx.cmd)
    url = remote if ("://" in remote or remote.startswith("git@")) \
        else R.git(ctx.cwd, "remote", "get-url", remote)
    if not url or "github.com" not in url.lower():
        return None  # non-GitHub, or a remote we cannot resolve — not this gate's concern
    if "x-access-token" in url:
        return None  # the sanctioned bot-token push (explicit token remote, e.g. agent-push.sh)
    if _bot_auth_active(ctx.cwd):
        return None  # transparent bot-auth worktree: SSH-form URL rewritten to an x-access-token push
    return ("deny",
            "A `git push` to GitHub from an agent session must authenticate as the bot, not over SSH "
            "or the human's stored credentials. Pushing as the human makes the HUMAN the \"last "
            "pusher\", and branch protection (require_last_push_approval) then bars them from approving "
            "the PR.\n\n"
            "Two sanctioned paths make the BOT the pusher:\n"
            "  • work in an AGENT WORKTREE (.claude/worktrees/…) — bot auth is automatic there, a plain "
            "`git push` just works (installed by lotek's scripts/install-bot-push-auth.sh); or\n"
            "  • scripts/agent-push.sh <refspec>      # e.g.  scripts/agent-push.sh HEAD:refs/heads/<branch>\n\n"
            f"Resolved push URL: {url}\n"
            "Exceptional bypass (e.g. pushing your OWN branch as yourself): prefix RAILS_OVERRIDE=1.")


def _read_marker(gd: str, basename: str) -> dict | None:
    """The marker dict, or None if absent/unreadable/malformed."""
    try:
        with open(os.path.join(gd, basename), encoding="utf-8") as fh:
            marker = json.load(fh)
    except (OSError, ValueError):
        return None
    return marker if isinstance(marker, dict) else None


def _head_marker_state(gd: str, basename: str, head: str, head_tree: str | None = None) -> str:
    """'ok' if the marker covers the current commit, 'stale' if it was recorded for different content
    (you've committed since), 'missing' otherwise. Ported verbatim from lotek — the content-based
    tree comparison (vs. a time-based check) is what lets a review ack the STAGED tree before the
    final commit and still count afterwards. See lotek's `_rails.py`/`rails_gate.py` for the full
    rationale; unchanged here because it's repo-agnostic."""
    marker = _read_marker(gd, basename)
    if marker is None:
        return "missing"
    if marker.get("head") == head:
        return "ok"
    if head_tree and marker.get("tree") == head_tree:
        return "ok"
    if marker.get("head") or marker.get("tree"):
        return "stale"
    return "missing"


def _marker_mode(gd: str, basename: str) -> str | None:
    marker = _read_marker(gd, basename)
    mode = marker.get("mode") if marker else None
    return mode if isinstance(mode, str) else None


# Each requirement gating `gh pr create`: (marker file, --ack flag, human label, how to satisfy it).
# NOTE: deliberately 4 entries, not 5 — no invariants requirement. See module docstring
# "Invariant divergence".
_PRE_PR_REQUIREMENTS = (
    (LOCAL_TESTS_MARKER_BASENAME, "--ack-tests", "local test run",
     "run the relevant subproject suite(s) locally — `cd <ext> && uv run --extra dev pytest -q` "
     "for each touched extension (cream/registrar/scribble/vector), plus `uvx ruff check <ext>` and "
     "`cd <ext> && uv run --extra dev pyrefly check .` (mirrors the clean-checks gate)"),
    (ADVERSARIAL_MARKER_BASENAME, "--ack-adversarial", "adversarial review",
     "run /adversarial-reviewer BEFORE you commit — review the staged tree, resolve BLOCK/CONCERNS, "
     "then `--ack-adversarial --staged` and commit (the marker binds to the tree, so it survives the "
     "commit). Already committed? Review `git diff main...HEAD` and ack without --staged"),
    (REVIEW_MARKER_BASENAME, "--ack-review", "security review",
     "run /security-review on `git diff main...HEAD` (the full PR diff), resolve findings, then ack — "
     "or review the staged tree before your final commit and `--ack-review --staged`. If this touches "
     "a core-reference id (principal/client/engagement/job/finding/asset/object), tenancy scoping, "
     "audit emission, or a confirm-tier verb, consult lotek core's INVARIANTS.md FIRST "
     f"({_LOTEK_INVARIANTS_URL}) — it is the canonical contract, not anything in this repo"),
    (TRANSCRIPTS_MARKER_BASENAME, "--ack-transcripts", "red-then-green transcripts",
     "break each guard this branch adds, watch it fail, fix it, show both in the PR body — then ack "
     "(no guard added? ack anyway and say so: the point is that someone decided)"),
)

# `--ack-transcripts` applies only to a branch touching some subproject's `tests/` dir. Unlike
# lotek core (single `tests/` at the repo root), tests here live under `<ext>/tests/` — so the
# check is "does any changed path have a `tests` PATH SEGMENT", not a fixed prefix.
_DOCS_ONLY_SUFFIXES = (".md",)


def _path_has_tests_segment(path: str) -> bool:
    return "tests" in path.split("/")


def _branch_is_docs_only(cwd: str) -> bool:
    """True only if EVERY path this branch changes against its base is documentation.

    Fails closed (returns False) on an unresolvable base ref / unreadable / empty diff, or any
    non-`.md` path. Ported from lotek — same rationale, same fail-closed direction."""
    for base in ("origin/main", "main"):
        merge_base = R.git(cwd, "merge-base", "HEAD", base)
        if not merge_base:
            continue
        out = R.git(cwd, "diff", "--name-only", merge_base, "HEAD")
        if out is None:
            return False
        paths = [line.strip() for line in out.splitlines() if line.strip()]
        return bool(paths) and all(p.endswith(_DOCS_ONLY_SUFFIXES) for p in paths)
    return False


def _branch_touches_tests(cwd: str) -> bool:
    """True if any path this branch changes against its base has a `tests` path segment
    (`cream/tests/...`, `scribble/tests/unit/...`, etc.). Fails closed (returns True) on an
    unresolvable base ref / unreadable diff, so the transcripts requirement stays in force rather
    than silently dropping."""
    for base in ("origin/main", "main"):
        merge_base = R.git(cwd, "merge-base", "HEAD", base)
        if not merge_base:
            continue
        out = R.git(cwd, "diff", "--name-only", merge_base, "HEAD")
        if out is None:
            return True
        return any(_path_has_tests_segment(line.strip()) for line in out.splitlines() if line.strip())
    return True


def _g_pre_pr_review(ctx: Ctx) -> Verdict:
    """Opening a PR (`gh pr create`) requires a local test pass, an adversarial review, and a
    security review, each recorded for the branch tip. Ported from lotek's `_g_pre_pr_review` —
    same marker mechanics, narrowed requirement set (no invariants; see module docstring)."""
    if not _runs_gh_pr_create(ctx.cmd):
        return None
    head = R.git(ctx.cwd, "rev-parse", "HEAD")
    gd = R.git_dir(ctx.cwd)
    if not head or not gd:
        ctx.log("pre-pr-review", "fail-open", "no HEAD / git-dir; cannot verify markers")
        return None
    head_tree = R.git(ctx.cwd, "rev-parse", "HEAD^{tree}")
    requirements = _PRE_PR_REQUIREMENTS
    if _branch_is_docs_only(ctx.cwd):
        requirements = tuple(r for r in requirements if r[0] != LOCAL_TESTS_MARKER_BASENAME)
        ctx.log("pre-pr-review", "warn", "docs-only branch: --ack-tests not required")
    if not _branch_touches_tests(ctx.cwd):
        requirements = tuple(r for r in requirements if r[0] != TRANSCRIPTS_MARKER_BASENAME)
        ctx.log("pre-pr-review", "warn", "branch touches no subproject's tests/ — --ack-transcripts not required")
    missing: list[str] = []
    for basename, flag, label, howto in requirements:
        tree = head_tree if basename in (ADVERSARIAL_MARKER_BASENAME, REVIEW_MARKER_BASENAME) else None
        state = _head_marker_state(gd, basename, head, tree)
        if state != "ok":
            why = "not recorded" if state == "missing" else \
                "recorded for an older commit (you've committed since)"
            missing.append(f"  • {label} — {why}.\n    {howto}, then:\n"
                           f"        python3 .claude/hooks/rails_gate.py {flag}")
        elif basename == ADVERSARIAL_MARKER_BASENAME and _marker_mode(gd, basename) == "post-commit":
            ctx.log("pre-pr-review", "warn",
                    "adversarial review acked AFTER the commit; review the staged tree and use "
                    "`--ack-adversarial --staged` next time")
    if not missing:
        return None
    return ("deny",
            "Opening a PR requires local verification recorded for this branch tip.\n\n"
            + "\n".join(missing)
            + "\n\nThen re-run `gh pr create`. Exceptional bypass (logged): prefix RAILS_OVERRIDE=1.")


def _lotek_invariants_hint() -> str:
    """A local INVARIANTS.md path if one is on disk, else the canonical URL. NEVER raises, and
    absence is NOT an error — this repo runs standalone without lotek core present."""
    for cand in _LOTEK_INVARIANTS_LOCAL_CANDIDATES:
        try:
            if os.path.isfile(cand):
                return cand
        except OSError:
            continue
    return _LOTEK_INVARIANTS_URL


def _g_invariant_pointer(ctx: Ctx) -> Verdict:
    """Non-blocking reminder on `gh pr create`: this repo has no local invariant registry — lotek
    core's INVARIANTS.md is the canonical contract. Always fires as `context` (never denies) so it
    can't turn into a cross-repo hard dependency; see "Invariant divergence" in the module
    docstring for why `--ack-invariants` doesn't gate this instead."""
    if not _runs_gh_pr_create(ctx.cmd):
        return None
    hint = _lotek_invariants_hint()
    return ("context",
            f"ℹ rails: lotek-extensions has no local INVARIANTS.md — the canonical security-invariant "
            f"contract lives in lotek core ({hint}). If this branch touches a core-reference id "
            "(principal/client/engagement/job/finding/asset/object), tenancy scoping, audit emission, "
            "or a confirm-tier verb, consult it before merging. Any inline `INV-…` tag in this repo's "
            "code (INV-EXT-*, INV-TENANCY-05/06, INV-INTEGRITY-03, INV-AUDIT-03/04, INV-SECRET-05, …) "
            "must name a REAL entry there — an INV- tag naming nothing is a defect.")


def _owning_subproject(path: str) -> str | None:
    top = path.split("/", 1)[0]
    return top if top in _SUBPROJECTS else None


def _staged_python(cwd: str, root: str) -> list[str]:
    out = R.git(cwd, "diff", "--cached", "--name-only", "--diff-filter=ACM") or ""
    return [f for f in out.splitlines() if f.endswith(".py") and os.path.exists(os.path.join(root, f))]


def _run(cmd: list[str], cwd: str) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=300)
    except Exception:
        return None  # checker couldn't launch — never block on infra failure


def _g_clean_checks(ctx: Ctx) -> Verdict:
    """ruff + pyrefly must be clean before a commit, scoped to the STAGED Python files (not lotek's
    whole-project `ruff check .` — this is a monorepo of independent subprojects, and a commit to
    `scribble/` has no business being blocked by an unrelated `vector/` lint issue, or vice versa).

    ruff runs once across every staged file: passing explicit repo-root-relative paths lets ruff's
    own config discovery pick each file's nearest `pyproject.toml` (each subproject has its own
    `[tool.ruff]`), so one invocation is correct even across subprojects.

    pyrefly has no such multi-project discovery — it's a per-subproject `uv` dev-dependency with
    its own venv/lock, so it runs ONCE PER SUBPROJECT that owns a staged file, with `cwd` set to
    that subproject's directory (`uv run pyrefly check <relpaths>`)."""
    if not ctx.is_commit():
        return None
    root = R.git(ctx.cwd, "rev-parse", "--show-toplevel")
    if not root:
        ctx.log("clean-checks", "fail-open", "could not resolve repo root")
        return None
    files = _staged_python(ctx.cwd, root)
    problems: list[str] = []

    if files:
        ruff_bin = shutil.which("ruff") or shutil.which("uvx")
        if ruff_bin is None:
            ctx.log("clean-checks", "fail-open", "ruff/uvx not found on PATH")
        else:
            argv = (["uvx", "ruff", "check", *files] if ruff_bin.endswith("uvx")
                    else ["ruff", "check", *files])
            result = _run(argv, root)
            if result is None:
                ctx.log("clean-checks", "fail-open", "ruff check could not launch")
            elif result.returncode != 0:
                out = ((result.stdout or "") + "\n" + (result.stderr or "")).strip()
                problems.append(f"ruff check failed:\n{out[-1800:]}")

    by_sub: dict[str, list[str]] = {}
    for f in files:
        sub = _owning_subproject(f)
        if sub:
            by_sub.setdefault(sub, []).append(f)
    if by_sub:
        if shutil.which("uv") is None:
            ctx.log("clean-checks", "fail-open", "uv not found; pyrefly check skipped")
        else:
            for sub, subfiles in sorted(by_sub.items()):
                sub_dir = os.path.join(root, sub)
                rel = [os.path.relpath(os.path.join(root, f), sub_dir) for f in subfiles]
                # Each subproject declares `pyrefly`/`ruff`/`pytest` under `[project.optional-
                # dependencies].dev`, NOT `[dependency-groups]` — so `uv run` does not pull them in
                # by default; `--extra dev` is required (same reason lotek's own CLAUDE.md calls
                # out `--extra extensions` when consuming these packages from the other side).
                result = _run(["uv", "run", "--extra", "dev", "pyrefly", "check", *rel], sub_dir)
                if result is None:
                    ctx.log("clean-checks", "fail-open", f"pyrefly check ({sub}) could not launch")
                    continue
                if result.returncode != 0:
                    out = ((result.stdout or "") + "\n" + (result.stderr or "")).strip()
                    problems.append(f"pyrefly check ({sub}) failed:\n{out[-1800:]}")

    if not problems:
        return None
    return ("deny",
            "Lint/type checks must pass before committing (ruff on the staged files; pyrefly per "
            "touched subproject).\n\n"
            + "\n\n".join(problems)
            + "\n\nFix the issues, then re-commit. Exceptional bypass: prefix RAILS_OVERRIDE=1.")


class Gate(NamedTuple):
    name: str
    check: Callable[[Ctx], Verdict]


GATES: list[Gate] = [
    Gate("explicit-staging", _g_explicit_staging),
    Gate("protected-branch", _g_protected_branch),
    Gate("push-identity", _g_push_identity),
    Gate("pre-pr-review", _g_pre_pr_review),
    Gate("invariant-pointer", _g_invariant_pointer),
    Gate("clean-checks", _g_clean_checks),
]

# --------------------------------------------------------------------- override map

def _applicable_override(cmd: str, gate: str) -> str | None:
    if R.has_token_assignment(cmd, R.CANONICAL_OVERRIDE):
        return R.CANONICAL_OVERRIDE
    for tok, owned_gate in R.LEGACY_OVERRIDES.items():
        if owned_gate == gate and R.has_token_assignment(cmd, tok):
            return tok
    return None

# ------------------------------------------------------------------------ dispatch

def evaluate(ctx: Ctx) -> None:
    for gate in GATES:
        verdict = gate.check(ctx)
        if verdict is None:
            continue
        kind, message = verdict
        if kind == "context":
            ctx.contexts.append(message)
            ctx.log(gate.name, "warn")
            continue
        ov = _applicable_override(ctx.cmd, gate.name)
        if ov:
            detail = f"bypassed via {ov}" + ("" if ov == R.CANONICAL_OVERRIDE else " (deprecated token)")
            ctx.log(gate.name, "override", detail)
            continue
        ctx.log(gate.name, "deny")
        R.emit_deny(message)
    if ctx.contexts:
        R.emit_context("\n\n".join(ctx.contexts))
    R.emit_allow()


def ack_head_marker(basename: str, label: str) -> int:
    """Record a HEAD-bound marker for the current commit. Also records ``HEAD^{tree}``, so an
    amend that does not change content keeps the marker valid."""
    cwd = os.getcwd()
    head = R.git(cwd, "rev-parse", "HEAD")
    gd = R.git_dir(cwd)
    if not head or not gd:
        sys.stderr.write(f"rails: not a git repo, or no HEAD — {label} not recorded.\n")
        return 1
    marker = {
        "head": head,
        "tree": R.git(cwd, "rev-parse", "HEAD^{tree}"),
        "mode": "post-commit",
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "branch": R.current_branch(cwd),
    }
    try:
        with open(os.path.join(gd, basename), "w", encoding="utf-8") as fh:
            json.dump(marker, fh)
    except OSError as exc:
        sys.stderr.write(f"rails: could not write {label} marker: {exc}\n")
        return 1
    print(f"rails: recorded {label} for HEAD {head[:12]} — `gh pr create` will pass "
          "(a new commit invalidates it; re-run then).")
    return 0


def ack_staged_marker(basename: str, label: str) -> int:
    """Record a marker against the STAGED tree, so the review can run BEFORE the commit."""
    cwd = os.getcwd()
    tree = R.git(cwd, "write-tree")
    gd = R.git_dir(cwd)
    if not tree or not gd:
        sys.stderr.write(f"rails: not a git repo, or `git write-tree` failed — {label} not recorded.\n")
        return 1
    marker = {
        "tree": tree,
        "mode": "pre-commit",
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "branch": R.current_branch(cwd),
    }
    try:
        with open(os.path.join(gd, basename), "w", encoding="utf-8") as fh:
            json.dump(marker, fh)
    except OSError as exc:
        sys.stderr.write(f"rails: could not write {label} marker: {exc}\n")
        return 1
    print(f"rails: recorded {label} for staged tree {tree[:12]} — commit it, and `gh pr create` will "
          "pass (staging more changes the tree and invalidates it; re-run then).")
    return 0


_INVARIANT_MARK_RE = re.compile(r"@pytest\.mark\.invariant\b")


def _scan_invariant_marks(repo_root: str) -> dict[str, int]:
    """{subproject: count of `@pytest.mark.invariant` occurrences under its tests/}."""
    counts: dict[str, int] = {}
    for sub in _SUBPROJECTS:
        tests_dir = os.path.join(repo_root, sub, "tests")
        if not os.path.isdir(tests_dir):
            continue
        n = 0
        for dirpath, _dirnames, filenames in os.walk(tests_dir):
            for name in filenames:
                if not name.endswith(".py"):
                    continue
                try:
                    with open(os.path.join(dirpath, name), encoding="utf-8") as fh:
                        n += len(_INVARIANT_MARK_RE.findall(fh.read()))
                except OSError:
                    continue
        if n:
            counts[sub] = n
    return counts


def ack_invariants() -> int:
    """INTENTIONAL DIVERGENCE from lotek core: see module docstring "Invariant divergence".

    This does NOT run a test suite and does NOT write a gating marker — there is no local
    `pytest -m invariant` contract to run. It only detects whether any `@pytest.mark.invariant`
    usage exists across the four subprojects and logs an honest SKIP to the audit trail: never a
    fake green, never a requirement. `gh pr create` does not consult this at all (it is not in
    `_PRE_PR_REQUIREMENTS`) — the `invariant-pointer` gate is what actually surfaces the reminder
    on every `gh pr create`.
    """
    cwd = os.getcwd()
    root = R.git(cwd, "rev-parse", "--show-toplevel") or cwd
    counts = _scan_invariant_marks(root)
    if counts:
        detail = ("found @pytest.mark.invariant usage: "
                  + ", ".join(f"{sub}={n}" for sub, n in sorted(counts.items()))
                  + " — but no `pytest -m invariant` contract is wired in this repo; this CLI does "
                    "NOT gate gh pr create on it (consider building a real gate before treating "
                    "these as equivalent to lotek core's INVARIANTS.md contract)")
    else:
        detail = ("no @pytest.mark.invariant usage found across cream/registrar/scribble/vector — "
                  "this repo has no local invariant registry (lotek core owns INVARIANTS.md); "
                  "--ack-invariants intentionally does not gate gh pr create here")
    R.audit(cwd, gate="invariants", action="skip", detail=detail)
    print(f"rails: {detail}")
    return 0


def main() -> None:
    if sys.argv[1:2] == ["--ack-review"]:
        extras = [a for a in sys.argv[2:] if a != "--staged"]
        if extras:
            sys.stderr.write(f"rails: unknown argument(s) for --ack-review: {' '.join(extras)}\n"
                             "       did you mean `--staged`? Nothing recorded.\n")
            sys.exit(2)
        if "--staged" in sys.argv[2:]:
            sys.exit(ack_staged_marker(REVIEW_MARKER_BASENAME, "security-review"))
        sys.exit(ack_head_marker(REVIEW_MARKER_BASENAME, "security-review"))
    if sys.argv[1:2] == ["--ack-tests"]:
        sys.exit(ack_head_marker(LOCAL_TESTS_MARKER_BASENAME, "local-tests"))
    if sys.argv[1:2] == ["--ack-adversarial"]:
        extras = [a for a in sys.argv[2:] if a != "--staged"]
        if extras:
            sys.stderr.write(f"rails: unknown argument(s) for --ack-adversarial: {' '.join(extras)}\n"
                             "       did you mean `--staged`? Nothing recorded.\n")
            sys.exit(2)
        if "--staged" in sys.argv[2:]:
            sys.exit(ack_staged_marker(ADVERSARIAL_MARKER_BASENAME, "adversarial-review"))
        sys.exit(ack_head_marker(ADVERSARIAL_MARKER_BASENAME, "adversarial-review"))
    if sys.argv[1:2] == ["--ack-transcripts"]:
        sys.exit(ack_head_marker(TRANSCRIPTS_MARKER_BASENAME, "guard-transcripts"))
    if sys.argv[1:2] == ["--ack-invariants"]:
        sys.exit(ack_invariants())

    data = R.read_input()
    cmd = R.bash_command(data)
    if not cmd:
        R.emit_allow()
    cwd = (data or {}).get("cwd") or os.getcwd()
    session_id = (data or {}).get("session_id")
    evaluate(Ctx(cmd, cwd, session_id if isinstance(session_id, str) else None))


if __name__ == "__main__":
    main()
