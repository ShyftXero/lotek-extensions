# sshim

A disposable **SSH-only pivot generator** for lotek — *revshells.com for `ssh`*. Two machines on
hostile networks rendezvous through an SSH relay using only the resident `ssh` client (no binary on
disk, runs on a borrowed box). When both sides hang up, it's gone. **Not persistent access.**

Origin use case: reach a target network from off-site — a consultant on a client LAN they do
not control, an operator at a coffee shop needing one host inside the engagement scope.

## Phase 1 (this) — zero core changes

The rendezvous is a **3rd-party box the operator controls** (a VPS, a tailnet node) — exactly the
revshells model. sshim needs **no lotek-core modification** and imports **nothing** from `app.*`: the
generator just emits commands pointing at a `rendezvous_host` the operator names. **There is no shipped
rendezvous default** — no host, no fixed port — because a well-known inbound port is precisely the
exposure this tool warns about; the mint page opens with a blank field.

**One credential model — a fresh identity per pivot:** a generated `ssh_user` (`p` + 8 hex, always a
leading letter, <=16 chars) plus an 18+ character random password, threaded into both legs. A profile
may pin a real account instead, and the pin wins. No key generation — `ssh -i` needs a key *file* on the
borrowed box, which is the residue a throwaway pivot must not leave.

The password is **stored sealed** (`sshim/secretbox.py`: an HMAC-SHA256 box keyed by a 0600 file in the
instance dir, never in the DB, never in a log) so its owner can re-fetch it from another borrowed box —
and **never cleartext in the row**, never on a command line, and **never in a list response**. A
missing/corrupt key fails closed. ⚠️ A generated identity only exists if something *provisions* it on the
rendezvous: an sshim relay container does (from `USER_NAME`/`USER_PASSWORD`); an external box needs a
pinned real user. sshim never creates accounts.

Phase 2 (lotek#1747) will later let a profile request a lotek-hosted single-use relay through the host
`extras` seam (`exclusive_container_acquire` / `exclusive_container_release`); `deps.host_hook` is the
single future wiring point and Phase 1 never calls it.

## What it does

- **Three topologies × four renderers** (`sshim/plan.py`, the folded + pinned generator):
  - A `relay_reverse_socks` — both sides behind NAT; remote DNS is free (the anchor resolves names).
  - B `direct_socks` — anchor is routable; `-D` resolves on the operator (stated, not hidden).
  - C `relay_sshuttle` — Linux operator wants full-subnet L3 (needs sudo + Python on the anchor).
  - Renderers: `posix` ssh, `ssh_exe`, `powershell_encoded`, `sshuttle`; plus a transport **encoding**
    post-transform (None / URL / URL-double / Base64).
- **Running-pivots list + re-fetch.** A minted session lives server-side (`sshim_sessions`); its full
  plan (both legs, every renderer) is regenerated on demand from the stored snapshot, and the identity is
  re-shown from its sealed row. Mint on one box, walk away, borrow another, pull the commands back.
- **Lifetime.** TTL as before, plus an **opt-in kill date** (`kill_at`, blank by default) that reports a
  terminal `killed` and refuses the plan once it passes. There is no core reaper in Phase 1, so sshim
  reaps itself: a throttled `before_request` sweep (30s per process) and `flask sshim-reap` for cron.
  The sweep is idempotent — `expired` stays derived, only `killed` is ever persisted.

## Security properties (preserved from the proven generator)

- The **password is never on argv** (pasted at the prompt) — so a stored session re-renders to
  byte-identical commands, and the secret is stored **sealed** rather than blanked: the owner's re-fetch
  shows it again, the lists never do.
- `UserKnownHostsFile=/dev/null` (POSIX) / `NUL` (Windows), `StrictHostKeyChecking=accept-new`,
  `ExitOnForwardFailure=yes`, loopback-only forwards.
- Tenancy: a profile/session is visible to its owner + an admin only, enforced in one place
  (`service.py`). Core-ref columns are `Uuid` (INV-INTEGRITY-03).

## Surfaces

- `GET /sshim/` — running pivots · `GET/POST /sshim/mint` (TTL + optional kill date) ·
  `GET /sshim/sessions/<id>` (re-fetch, reveals the owner's identity) ·
  `POST /sshim/sessions/<id>/kill` · `GET/POST /sshim/profiles`.
- CLI: `flask sshim-reap` — force the expiry sweep (cron-friendly).
- Machine API (PAT): `GET /sshim/machine/sessions`, `GET /sshim/machine/sessions/<id>` (full plan),
  `POST /sshim/machine/sessions`, `POST /sshim/machine/sessions/<id>/kill`. The **list** never
  serialises the password; only the single-session detail (owner/admin) reveals it.

## Develop

```sh
uvx ruff check sshim
cd sshim && uv run --extra dev pytest -q
```
