#!/usr/bin/env python3
"""Mint a short-lived GitHub App installation token for agent sessions (lotek-extensions).

PURPOSE
-------
Agents commit and open PRs as ``lotek-agent[bot]``, never as the human. That split is what makes
``required_approving_review_count: 1`` a *real* gate rather than self-approval: GitHub refuses to let a
PR's author approve their own PR, so a human approval only means something when the author is the bot.
This script mints the installation token an agent uses to push and to run ``gh pr create`` as the bot.

This is a copy of lotek's ``scripts/gh-app-token.py`` — the SAME GitHub App (``lotek-agent``) and the
SAME installation serve both ``ShyftXero/lotek`` and ``ShyftXero/lotek-extensions``, and the credentials
live once at ``~/.config/lotek-agent/``. It is kept here so a session working only in this repo has the
tool locally instead of reaching into the lotek checkout.

USAGE
-----
    scripts/gh-app-token.py --check     # verify IDs + permissions + repo reach; prints NO token
    scripts/gh-app-token.py             # print a valid token to stdout (for $(...) capture)
    eval "$(scripts/gh-app-token.py --export)"   # set GH_TOKEN in the current shell
    scripts/gh-app-token.py --identity  # print the bot's git author/committer name + noreply email

Open a bot-authored PR in ONE shell call — the token does NOT persist between separate Bash calls, and
an SSH push would record the HUMAN as the last pusher, so mint + push (over HTTPS with the token) +
create the PR together:

    GH_TOKEN=$(scripts/gh-app-token.py); \\
      git -c http.extraheader="AUTHORIZATION: bearer $GH_TOKEN" \\
          push https://x-access-token:$GH_TOKEN@github.com/ShyftXero/lotek-extensions.git HEAD && \\
      GH_TOKEN=$GH_TOKEN gh pr create --base main --head <branch> --title "…" --body "…"

Installation tokens expire after one hour; the script caches one mode-0600 and re-mints only near
expiry, so the expiry is invisible in normal use.

REPO SCOPING  (why the first monorepo PR fell back to the human account)
------------------------------------------------------------------------
An installation token reaches exactly the repos the App is granted access to AT MINT TIME. The
``lotek-agent`` App must be installed on THIS repo for the bot to push / open PRs here. It was NOT until
2026-08-04 — so the first monorepo PR (#1, ``feat/fraction-checklists``) had to be opened by the human
and then could not be self-approved (the deadlock this whole mechanism exists to avoid). If a fresh
``--check`` prints "the token cannot reach …", the App lacks access to this repo:

    Settings > Applications > Installed GitHub Apps > lotek-agent > Configure
      > Repository access > add `lotek-extensions` (or All repositories) > Save

then delete the cached token (``~/.config/lotek-agent/token.json``) so the next mint picks up the grant
(an already-minted token never gains a repo added after it was issued).

CONFIGURATION  (env vars; defaults suit this org + this repo)
-------------------------------------------------------------
    LOTEK_GH_APP_ID           default 4451736
    LOTEK_GH_INSTALLATION_ID  default 150438876
    LOTEK_GH_APP_KEY          default ~/.config/lotek-agent/app.pem    (must be mode 0600)
    LOTEK_GH_TOKEN_CACHE      default ~/.config/lotek-agent/token.json (written mode 0600)
    LOTEK_GH_REPO             default ShyftXero/lotek-extensions — what `--check` proves reachability
                              against (this copy defaults to THIS repo; set ShyftXero/lotek to check that).

Requires the ``cryptography`` package at runtime (it signs the RS256 App JWT). The private key never
leaves this process and is never logged; ``--check`` prints only metadata.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import json
import os
import stat
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

APP_ID = os.environ.get("LOTEK_GH_APP_ID", "4451736")
INSTALLATION_ID = os.environ.get("LOTEK_GH_INSTALLATION_ID", "150438876")
KEY_PATH = Path(os.environ.get("LOTEK_GH_APP_KEY", "~/.config/lotek-agent/app.pem")).expanduser()
CACHE_PATH = Path(os.environ.get("LOTEK_GH_TOKEN_CACHE", "~/.config/lotek-agent/token.json")).expanduser()
#: What `--check` proves reachability against. This copy lives in the monorepo, so it defaults to the
#: monorepo; override with LOTEK_GH_REPO=ShyftXero/lotek to check the framework repo from here.
TARGET_REPO = os.environ.get("LOTEK_GH_REPO", "ShyftXero/lotek-extensions")

API = "https://api.github.com"
#: Re-mint with this much life left, so a long push can't have the token die underneath it.
REFRESH_MARGIN_S = 600

#: Everything the agent workflow needs. `workflows` is easy to forget and fails confusingly on push
#: (any commit touching .github/workflows/ is refused), so it is checked explicitly.
REQUIRED_PERMISSIONS = {"contents": "write", "pull_requests": "write", "workflows": "write"}


def _b64(raw: bytes) -> bytes:
    return base64.urlsafe_b64encode(raw).rstrip(b"=")


def _app_jwt() -> str:
    """A 9-minute RS256 assertion identifying the App itself (GitHub's max is 10)."""
    if not KEY_PATH.exists():
        sys.exit(f"error: private key not found at {KEY_PATH} (set LOTEK_GH_APP_KEY)")
    mode = stat.S_IMODE(KEY_PATH.stat().st_mode)
    if mode & 0o077:
        sys.exit(f"error: {KEY_PATH} is mode {mode:04o} — group/other can read an App private key. "
                 f"Run: chmod 600 {KEY_PATH}")
    key = serialization.load_pem_private_key(KEY_PATH.read_bytes(), password=None)
    now = int(time.time())
    header = _b64(json.dumps({"alg": "RS256", "typ": "JWT"}, separators=(",", ":")).encode())
    payload = _b64(json.dumps({"iat": now - 60, "exp": now + 540, "iss": APP_ID},
                              separators=(",", ":")).encode())
    signing_input = header + b"." + payload
    signature = _b64(key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256()))
    return (signing_input + b"." + signature).decode()


def _api(path: str, token: str, *, method: str = "GET", bearer: bool = True) -> dict:
    req = urllib.request.Request(
        f"{API}{path}",
        method=method,
        headers={
            "Authorization": f"{'Bearer' if bearer else 'token'} {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "lotek-agent",
        },
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")[:400]
        sys.exit(f"error: {method} {path} -> HTTP {exc.code}\n{body}")


def write_cache(tok: dict, path: Path | None = None) -> Path:
    """Persist a minted token owner-only. Separated from `_mint` so it is testable WITHOUT a network
    call — the permission behaviour here is the security-relevant part and it needs a real guard, not a
    one-off shell transcript (see tests/test_gh_app_token_cache_perms.py in lotek).

    INV-SECRET-03, both halves. Atomic create is NECESSARY but NOT SUFFICIENT:

      * never write-then-chmod — between those calls the file exists at the umask default (0644, or
        0664 under this host's umask of 002) and any local user can read the token; but also
      * never ADOPT a path that is already there. os.open's mode argument applies ONLY on creation,
        so O_CREAT onto a pre-existing 0666 token.json writes the credential straight into it and the
        0600 is silently ignored. Unlink first, then O_EXCL guarantees we created it.
      * O_NOFOLLOW (implied by O_EXCL, kept explicit) refuses a planted symlink, which would otherwise
        redirect the write to an attacker-chosen destination.

    The credential at stake holds contents:write + workflows:write on the repo.
    """
    p = path or CACHE_PATH
    p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(p.parent, 0o700)  # mkdir's mode applies only on creation, and umask reduces it
    with contextlib.suppress(FileNotFoundError):
        os.unlink(p)
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        os.write(fd, json.dumps(tok).encode())
    finally:
        os.close(fd)
    return p


def _mint() -> dict:
    jwt = _app_jwt()
    tok = _api(f"/app/installations/{INSTALLATION_ID}/access_tokens", jwt, method="POST")
    write_cache(tok)
    return tok


def _cached() -> dict | None:
    if not CACHE_PATH.exists():
        return None
    try:
        tok = json.loads(CACHE_PATH.read_text())
        expiry = time.strptime(tok["expires_at"], "%Y-%m-%dT%H:%M:%SZ")
        if time.mktime(expiry) - time.mktime(time.gmtime()) > REFRESH_MARGIN_S:
            return tok
    except Exception:
        pass  # unreadable/expired cache is not an error — just mint a new one
    return None


def _token() -> str:
    return (_cached() or _mint())["token"]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--check", action="store_true", help="verify config and permissions; print NO token")
    g.add_argument("--export", action="store_true", help="print `export GH_TOKEN=…` for eval")
    g.add_argument("--identity", action="store_true", help="print the bot's git user.name / user.email")
    args = ap.parse_args()

    if args.check:
        jwt = _app_jwt()
        app = _api("/app", jwt)
        print(f"app          : {app['slug']!r} (id {app['id']}, owner {app['owner']['login']})")
        insts = _api("/app/installations", jwt)
        print(f"installations: {[(i['id'], i['account']['login']) for i in insts]}")
        if not any(str(i["id"]) == str(INSTALLATION_ID) for i in insts):
            sys.exit(f"error: installation {INSTALLATION_ID} is not one of this App's installations")
        tok = _mint()
        print(f"token        : minted OK, expires {tok['expires_at']}")
        # Verify access by USING the token, not by reading the response shape. A token minted without a
        # `repositories` field in the POST body reaches every repo the installation has, and the response
        # then omits the `repositories` key entirely — so treating a missing/empty array as "no access"
        # is a false positive. It reported exactly that against a correctly-configured installation.
        try:
            _api(f"/repos/{TARGET_REPO}", tok["token"], bearer=False)
            print(f"repo access  : {TARGET_REPO} reachable (selection={tok.get('repository_selection')})")
        except SystemExit:
            print(f"\nMISSING      : the token cannot reach {TARGET_REPO}.")
            print("Fix at Settings > Applications > Installed GitHub Apps > lotek-agent > Configure,")
            print("under 'Repository access' — select the repo (or All repositories) and Save,")
            print("then delete ~/.config/lotek-agent/token.json so the next mint picks up the grant.")
            return 1
        perms = tok.get("permissions", {})
        print(f"permissions  : {json.dumps(perms, sort_keys=True)}")
        missing = [f"{k}:{v}" for k, v in REQUIRED_PERMISSIONS.items() if perms.get(k) != v]
        if missing:
            print(f"\nMISSING      : {', '.join(missing)}")
            print("Fix in Settings > Developer settings > GitHub Apps > lotek-agent > Permissions,")
            print("then ACCEPT the new permissions on the installation (GitHub will not apply them")
            print("until the installation approves the change).")
            return 1
        # /users/<login> is a USER endpoint: an App JWT is rejected there with 401. Use the
        # installation token instead.
        bot = _api(f"/users/{app['slug']}%5Bbot%5D", tok["token"], bearer=False)
        print(f"\ngit identity : {app['slug']}[bot] "
              f"<{bot['id']}+{app['slug']}[bot]@users.noreply.github.com>")
        print("OK — all required permissions present.")
        return 0

    if args.identity:
        jwt = _app_jwt()
        app = _api("/app", jwt)
        bot = _api(f"/users/{app['slug']}%5Bbot%5D", _token(), bearer=False)
        print(f"{app['slug']}[bot]")
        print(f"{bot['id']}+{app['slug']}[bot]@users.noreply.github.com")
        return 0

    if args.export:
        print(f"export GH_TOKEN={_token()}")
        return 0

    print(_token())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
