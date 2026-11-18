# Plan: feat/sshim

- **Branch:** `feat/sshim`  (worktree: `.dsa/01M7PJ1G4C27HDM651CP6WPRJX/build-sshim`, off `main`)
- **PR:** https://github.com/ShyftXero/lotek-extensions/pull/371
- **Status:** 🟢 Phase-1 review fixes landed (`9ca82d3` + the fix commit) — needs The Plague
- **Review fixes (PR #371, BUILD SCOPE):** no rendezvous default · deviation-risk banner · 18-char
  ephemeral-password model pinned · Phase-2 `exclusive_container_acquire` seam documented, not called

## Purpose
`sshim` — a disposable SSH-only pivot generator (revshells.com for `ssh`). Two machines on hostile
networks rendezvous through an SSH relay using only the resident `ssh` client; when both sides hang
up, it's gone. **Phase 1 only: ZERO lotek-core changes.** The rendezvous is a 3rd-party box the
operator configures in a profile (VPS / tailnet — **no shipped default**). sshim imports NOTHING from
`app.*`.

Folds the proven pure-Python command generator (`/tmp/sshim/sshim_gen.py`, 11/11 tested) in as
`sshim/plan.py` — 3 topologies × 4 renderers, password never on argv, `UserKnownHostsFile=/dev/null|NUL`,
`ExitOnForwardFailure=yes`, remote DNS free in topology A.

## Surfaces
- **Session list** `GET /sshim/` — every active + recently-expired pivot (so a TTL death is visible,
  not a mystery 404). Mono-led brutalist table, lime active dot.
- **Mint** `GET/POST /sshim/mint` — opens to defaults (topology A, Windows target ssh.exe, Linux
  operator ssh, 30min TTL) with a **blank rendezvous** the operator must fill (no host, no fixed port
  ships). A blank one re-renders the form with `configure a rendezvous host` at 400. Two copy buttons.
- **Session detail** `GET /sshim/sessions/<id>` — the re-fetch surface: regenerates the FULL plan
  (both legs, every applicable renderer) from the stored session via `render_plan`. Two-pane,
  copy/Raw/+1-port/encoding per leg, topology diagram, TTL countdown bar.
- **Kill** `POST /sshim/sessions/<id>/kill`.
- **Profiles** `GET/POST /sshim/profiles`, `POST /sshim/profiles/<id>/delete` (+ `?q=` search).
- **Machine API** `GET /sshim/machine/sessions`, `GET /sshim/machine/sessions/<id>` (full plan),
  `POST /sshim/machine/sessions` (mint), `POST /sshim/machine/sessions/<id>/kill` — PAT-gated twin.

## Key architecture decisions
- **Password is never stored.** `render_plan` keeps the secret off every command by design, so
  regenerating a stored session needs no secret. The session is minted with a one-time password shown
  once; the row stores only what regenerates the *commands* (profile snapshot JSON, ports, ttl,
  expires_at, status). Re-fetch rebuilds identical commands; the UI states plainly the secret can't be
  re-shown and offers re-mint. Never silently shows a stale/empty one.
- **One liveness predicate, one home.** `plan.effective_status(status, expires_at, now)` →
  `killed`>`expired`>`active`. Stored status only records the explicit operator action (`active`/
  `killed`); `expired` is DERIVED, never stored (no sweeper, no drift). `plan.is_listable(...)` is the
  single list-visibility predicate (active + recently-expired within grace). Every surface calls these.
- **One tenancy predicate, one home.** `service.visible_sessions` / `load_visible` — owner-only, admin
  sees all, no engagement_id (INV-TENANCY-06 explicit declared rule). Browser + machine surfaces feed
  the SAME functions; a session the caller may not see is 404, never 403 (INV-TENANCY-01).
- **Core-ref columns are `Uuid`** (owner_id), never Integer/String (INV-INTEGRITY-03).
- **No rendezvous default, ever.** `deps.DEFAULT_RELAY_HOST == ""` and the manifest
  `[[settings]] default_relay_host` default is `""`. A fixed host on a well-known inbound port is the
  exposure this tool exists to warn about, and Phase 1's rendezvous is the operator's own box — a
  shipped default could only point at a Phase-2 resource. The mint page's port field shows sshd's `22`
  (transparent, not hidden), and the risk banner flags it the moment it is used on a public host.
- **One deviation-risk predicate, one home.** `plan.deviation_risks(profile, credential=…)` → plain
  sentences; `plan.inbound_surface` decides WHICH port is inbound (rendezvous for A/C, anchor for B).
  Both the mint page and the session detail render `templates/sshim/_risk.html` from that same tuple,
  so they cannot disagree, and the banner is **silent** on the secure default (random port + one-use
  ephemeral password). Amber `.risk` style, deliberately distinct from the lime `.warn` secret note. It
  never blocks: it names the risk. An unknown credential kind RAISES rather than passing quietly, so a
  future static-credential model cannot slip in un-warned.
- **One credential model: a >=18-char random one-use password.** `plan.gen_password()` is
  `token_urlsafe(18)` = 24 URL-safe chars, pinned by `MIN_PASSWORD_LENGTH`. NO key generation — `ssh -i`
  needs a key file on the borrowed box, which is the residue a throwaway pivot must not leave.
- **Phase 2 seam kept clean, documented, NOT called.** `deps.exclusive_container_acquire()` /
  `exclusive_container_release()` are documented, unused accessors over `deps.host_hook` (lotek#1747's
  general container seam), with the landing comment at the mint call site in `blueprint.mint`. No
  `app.*` import, no dead code that runs: Phase 1 emits a command for the operator's own box.

## Done
- [x] Branch + worktree identity + plan file
- [x] `plan.py` (folded generator + encoding + liveness predicates)
- [x] `models.py`, `db.py`, `config.py`, `deps.py`, `host.py`
- [x] `service.py` (tenancy + mint + regen + kill + profiles)
- [x] `blueprint.py`, `api_pat.py`, `__init__.py`, manifest, pyproject
- [x] templates (base tokens, sessions, mint, detail, profiles) — Unkey dark/mono/lime
- [x] tests (plan, manifest, no-app-import, service) — 41 green
- [x] GATE: ruff clean, pyrefly clean, pytest 41 green; standalone mount smoke (routes render,
      mint works, re-fetch secret-free, machine API fail-closed 503)
- [x] Review fix 1: `DEFAULT_RELAY_HOST = ""`, manifest default `""`, blank mint field, clear
      `configure a rendezvous host` message (400 re-renders the form with the operator's values)
- [x] Review fix 2: `plan.deviation_risks` + `templates/sshim/_risk.html` on mint AND detail, amber
      `.risk`; fires on a pinned well-known port (22/443/…) or a static credential, silent on default
- [x] Review fix 3: 18+ char one-use password model pinned by test (`MIN_PASSWORD_LENGTH`)
- [x] Review fix 4: `deps.exclusive_container_acquire/release` documented + unused; mint call-site
      comment names the Phase-2 landing
- [x] GATE (post-fix): ruff + pyrefly clean, pytest 68 green (new: `tests/test_blueprint_risk.py`,
      risk-predicate + password tests in `test_plan.py`, blank-default test in `test_manifest.py`)

## Remaining
- [ ] The Plague review (Blade UX pass on the UI diff first), then self-merge `--merge`
- [ ] Phase 2 (separate branch, lotek#1747): call `deps.exclusive_container_acquire` at mint, mounted
      lotek-side `test_sshim_extension.py`

## Notes / gotchas
- `render_plan` does NOT use the password — reconstructing a `Session` with `rendezvous_password=""`
  yields byte-identical commands. That is what makes Phase-1 re-fetch correct and secret-free.
- Mounted behaviour (tenancy, PAT seam) is only truly proven in lotek's suite
  (`test_sshim_extension.py`) — a stub host proves logic, not the mount. Flagged for the lotek side.
- UI polish (the Unkey dark/mono/lime aesthetic) is Blade's lane — tokens + structure landed here;
  Blade should do the final UX pass on the PR (the new amber `.risk` banner is Blade's to restyle).
- `uvx ruff check sshim` in this environment resolves ruff **0.6.9**, which cannot parse
  `target-version = "py314"` (exit 2, config error, not a lint finding). The real gate here is the
  subproject's pinned ruff (`uv run --extra dev ruff check`), 0.17.5 → clean.
