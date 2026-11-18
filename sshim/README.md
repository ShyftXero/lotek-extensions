# sshim

A disposable **SSH-only pivot generator** for lotek — *revshells.com for `ssh`*. Two machines on
hostile networks rendezvous through an SSH relay using only the resident `ssh` client (no binary on
disk, runs on a borrowed box). When both sides hang up, it's gone. **Not persistent access.**

Origin use case: view a 3D printer's web UI from a hotel.

## Phase 1 (this) — zero core changes

The rendezvous is a **3rd-party box the operator controls** (a VPS, a tailnet node, or the `shyft.us`
CNAME) — exactly the revshells model. sshim needs **no lotek-core modification** and imports **nothing**
from `app.*`: the generator just emits commands pointing at a `rendezvous_host` the operator configured
in a profile. Phase 2 (lotek#1747) will later let a profile request a lotek-hosted ephemeral relay
through the host `extras` seam when it lands; `deps.host_hook` is the single future wiring point.

## What it does

- **Three topologies × four renderers** (`sshim/plan.py`, the folded + pinned generator):
  - A `relay_reverse_socks` — both sides behind NAT; remote DNS is free (the anchor resolves names).
  - B `direct_socks` — anchor is routable; `-D` resolves on the traveler (stated, not hidden).
  - C `relay_sshuttle` — Linux traveler wants full-subnet L3 (needs sudo + Python on the anchor).
  - Renderers: `posix` ssh, `ssh_exe`, `powershell_encoded`, `sshuttle`; plus a transport **encoding**
    post-transform (None / URL / URL-double / Base64).
- **Running-pivots list + re-fetch.** A minted session lives server-side (`sshim_sessions`); its full
  plan (both legs, every renderer) is regenerated on demand from the stored snapshot. Mint on one box,
  walk away, borrow another, pull the commands back.

## Security properties (preserved from the proven generator)

- The relay **password is never on argv** (pasted at the prompt) — and because it is never on a
  command, a stored session re-renders to byte-identical commands **without the secret**. The secret is
  shown once at mint and never stored; the re-fetch UI says so and offers a re-mint.
- `UserKnownHostsFile=/dev/null` (POSIX) / `NUL` (Windows), `StrictHostKeyChecking=accept-new`,
  `ExitOnForwardFailure=yes`, loopback-only forwards.
- Tenancy: a profile/session is visible to its owner + an admin only, enforced in one place
  (`service.py`). Core-ref columns are `Uuid` (INV-INTEGRITY-03).

## Surfaces

- `GET /sshim/` — running pivots · `GET/POST /sshim/mint` · `GET /sshim/sessions/<id>` (re-fetch) ·
  `POST /sshim/sessions/<id>/kill` · `GET/POST /sshim/profiles`.
- Machine API (PAT): `GET /sshim/machine/sessions`, `GET /sshim/machine/sessions/<id>` (full plan),
  `POST /sshim/machine/sessions`, `POST /sshim/machine/sessions/<id>/kill`.

## Develop

```sh
uvx ruff check sshim
cd sshim && uv run --extra dev pytest -q
```
