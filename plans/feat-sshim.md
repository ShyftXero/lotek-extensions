# Plan: feat/sshim

- **Branch:** `feat/sshim`  (worktree: `.dsa/01M7PJ1G4C27HDM651CP6WPRJX/build-sshim`, off `main`)
- **PR:** <opening>
- **Status:** 🟢 ready for review (needs The Plague)

## Purpose
`sshim` — a disposable SSH-only pivot generator (revshells.com for `ssh`). Two machines on hostile
networks rendezvous through an SSH relay using only the resident `ssh` client; when both sides hang
up, it's gone. **Phase 1 only: ZERO lotek-core changes.** The rendezvous is a 3rd-party box the
operator configures in a profile (VPS / tailnet / `shyft.us`). sshim imports NOTHING from `app.*`.

Folds the proven pure-Python command generator (`/tmp/sshim/sshim_gen.py`, 11/11 tested) in as
`sshim/plan.py` — 3 topologies × 4 renderers, password never on argv, `UserKnownHostsFile=/dev/null|NUL`,
`ExitOnForwardFailure=yes`, remote DNS free in topology A.

## Surfaces
- **Session list** `GET /sshim/` — every active + recently-expired pivot (so a TTL death is visible,
  not a mystery 404). Mono-led brutalist table, lime active dot.
- **Mint** `GET/POST /sshim/mint` — opens to defaults (topology A, shyft.us:443, Windows target
  ssh.exe, Linux operator ssh, random port, 30min TTL). Produces two copy buttons.
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
- **Phase 2 seam kept clean, not built.** No `ephemeral_relay_acquire/release`, no `app.pivots`. A
  profile's rendezvous is operator-configured text today; Phase 2 (lotek#1747) will later let a profile
  request a lotek-hosted relay through `extras` when that seam lands. `deps.host_hook` is the single
  future wiring point.

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

## Remaining
- [ ] The Plague review (Blade UX pass on the UI diff first), then self-merge `--merge`
- [ ] Phase 2 (separate branch, lotek#1747): ephemeral_relay seam, mounted lotek-side
      `test_sshim_extension.py`

## Notes / gotchas
- `render_plan` does NOT use the password — reconstructing a `Session` with `rendezvous_password=""`
  yields byte-identical commands. That is what makes Phase-1 re-fetch correct and secret-free.
- Mounted behaviour (tenancy, PAT seam) is only truly proven in lotek's suite
  (`test_sshim_extension.py`) — a stub host proves logic, not the mount. Flagged for the lotek side.
- UI polish (the Unkey dark/mono/lime aesthetic) is Blade's lane — tokens + structure landed here;
  Blade should do the final UX pass on the PR.
