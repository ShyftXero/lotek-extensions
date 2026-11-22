# sshim — SSH pivot generator

A disposable **SSH-only pivot**: a poor-man's VPN to a target network over `ssh`, for a quick task.
Two machines on hostile networks rendezvous through an SSH relay using only the resident `ssh` client —
no binary on disk, runs on a borrowed box. When both sides hang up, it's gone. **Not persistent access.**

## How to use it

1. **Pick a rendezvous.** Phase 1: a 3rd-party box *you* control — a VPS or a tailnet node. **sshim
   ships no rendezvous default**: the mint page opens with a blank host field and no fixed port, and
   minting without one fails with "configure a rendezvous host". (An admin can pre-fill a default via
   the `default_relay_host` setting; it is blank out of the box.)
2. **Mint a pivot** (`/sshim/mint`). Choose a topology, confirm the rendezvous, set a TTL (optionally a
   kill date). Every session gets its **own randomly generated SSH identity** — a fresh username and a
   fresh 18+ character password — and both are shown on the response page. Paste the password at the
   `ssh` prompt: it is never put on the command line, where `ps` could read it.
3. **Run the two legs** — the *anchor* (inside the target network, default Windows `ssh.exe`) and the
   *operator* (you, default Linux `ssh`). Copy each with its Copy button. Both legs dial the SAME
   per-session identity.
4. **Re-fetch anytime** from the running-pivots list (`/sshim/`). The commands are regenerated from the
   stored session, so you can pull them back on a different borrowed box — **and so is the identity**:
   the password is stored sealed (see below) and re-shown to its owner.
5. **Act on several at once.** The list sorts/filters/paginates through the host's shared data-table
   component, and has a leading checkbox column. Check rows (or the header box for the current filter),
   press **Kill N pivots**, and confirm the named targets — the confirmation lists profile + rendezvous
   + port, and the result page names every id that did **not** die. A selection survives filtering and
   paging: hiding a row does not deselect it.

## Topologies

| | mode | when | DNS |
|-|------|------|-----|
| **A** | relay + reverse SOCKS5 | both sides behind NAT | **remote** (anchor resolves) — free |
| **B** | direct SOCKS | anchor is routable (public IP / tailnet) | local (operator) — *not* remote |
| **C** | relay + sshuttle | Linux operator wants full-subnet L3 | remote; needs sudo + Python on anchor |

## What it is NOT

SSH-only. No shell, no staged payload, no bind/reverse shell, no MSFVenom/meterpreter. sshim builds
`ssh` forward commands and nothing else.

## Residue

`ssh` writes nothing to `known_hosts` (`UserKnownHostsFile=/dev/null`|`NUL`), accepts the host key on
first use, and forwards loopback-only. The one residue on a borrowed box is **shell history** — the UI
says so and offers the `powershell -EncodedCommand` renderer to keep the cleartext out of it.

## Phase 2 (not built)

A profile will later be able to request a **lotek-hosted single-use relay** instead of an operator box,
through the `exclusive_container_acquire`/`exclusive_container_release` host seam (lotek#1747). Phase 1
works entirely without it and never calls either hook (`deps.exclusive_container_acquire` is documented
and unused, so the next slice has one obvious place to land).

## The deviation-risk banner

A pivot is safest when its inbound port is random and its credential is a one-use secret. sshim states
plainly when a configuration departs from that, on **both** the mint page and the session detail:

- the inbound (rendezvous, or anchor for direct mode) port is a fixed, well-known service port — 22, 443
  and friends — on a public host; and/or
- the credentials are static: a reusable password, a saved profile carrying one, or a non-ephemeral key.

It is silent for the secure default (random port, one-use 18-character ephemeral password) and it never
blocks the action — it names the risk and lets the operator accept it. One predicate decides it
(`sshim/plan.py:deviation_risks`), so the two pages cannot disagree.

## Credential model — one identity per pivot

Every session gets its **own** randomly generated low-privilege identity instead of a shared literal
user:

- **`ssh_user`** — `p` + 8 hex (9 chars). It must start with a **letter** (Linux `useradd` rejects a
  leading digit; Windows OpenSSH accounts follow the same shape) and stays under the 16-character
  ceiling both accept.
- **`ssh_password`** — a random, URL-safe secret of **at least 18 visible characters**
  (`plan.gen_password`, 24 in practice).

Both are threaded into **every** generated command, for **both** legs (operator and anchor dial the same
rendezvous identity). A profile can still pin a real account — an **explicit** `rendezvous_user` /
`anchor_user` wins over the generated one (`plan.rendezvous_identity` / `plan.anchor_identity` are the
only places that decide this). Leave the user field blank on the mint form to get the generated one.

No key generation: `ssh -i` needs a key file on the borrowed box, which defeats the point of a
throwaway pivot.

### Where the password lives (it changed)

⚠️ sshim used to say "the password is shown once and never stored". That rule is **reversed**: the
borrowed-box re-fetch case needs it back, so `sshim_sessions.ssh_password` holds it **sealed**.

- The ciphertext comes from `sshim/secretbox.py`: a 32-byte key created **0600 in the instance dir** on
  first use, HMAC-SHA256 as both keystream and authentication tag. The key is never in the DB, never in
  a log, never in the package. (An extension may not import core's `EncryptedToken`.)
- It is **never cleartext in the row**, and it is **never on a command line** — so re-rendering a stored
  session still produces byte-identical commands.
- A missing or corrupt key **fails closed**: the detail page says the secret could not be unsealed and
  offers a re-mint, instead of printing garbage at a prompt.
- Only the **single-session detail** (owner or admin) reveals it. **List surfaces — including
  `GET /sshim/machine/sessions` — never carry it all.**

### What this does NOT do — read before promising anyone a login

A generated `ssh_user`/`ssh_password` only works if something **provisions that account** on the
rendezvous host. **sshim does not create accounts.**

- An **sshim-managed relay container** provisions it from the `USER_NAME` / `USER_PASSWORD` env vars —
  the `lotek-pivot-sshd` image already supports this.
- An **externally-owned rendezvous** (a VPS, the lotek host's own sshd) gets nothing from sshim: it
  cannot mint an account there. The operator must **pin a real user** in the profile (or accept that the
  pasted credential must already exist on that box).

The UI never implies sshim created an account it did not.

## Bulk kill

`POST /sshim/sessions/bulk-kill` takes repeated `ids` fields and kills each pivot through the SAME
per-pivot path as the single kill: `load_visible` per id (an unknown *or* not-yours id is `not_found`,
indistinguishable — no existence oracle), one audit entry **per pivot**, and `give_back_relay` per id.
The response is a **per-id** result (`killed` / `not_found`), so a partial failure names exactly which
pivots died and which did not; it never reports a batch as a success. The machine twin is
`POST /sshim/machine/sessions/bulk-kill` with `{"ids": ["…"]}`, returning the same shape plus
`killed` / `failed` counts.

The confirmation is **server-rendered** (the single-pivot kill's `confirm=1` gate at scale), so it is
not a client-side nicety: nothing is torn down until a second request carries every listed id back.

How the table sorts: Status uses the lifecycle order **active → expired → killed**
(`plan.status_rank`), and Expires / Kill at sort on the **ISO timestamp** (`data-sort-value`), not the
rendered `HH:MM:SS` — a clock alone is wrong across days.

## Lifetime — TTL plus an opt-in kill date

`ttl_minutes` works exactly as before (clamped to the admin `max_ttl_minutes`). On top of it a session
can carry a **`kill_at`** — an optional, explicit bound set by the mint form's "Kill at" field
(`datetime-local`, read as **UTC**; blank = none):

- a session whose `kill_at` has passed reports the terminal status **`killed`** and its commands stop
  being served (the detail page refuses the plan; the machine detail returns `409 session_dead`);
- a TTL-expired session behaves as it always did — it reads `expired` and stays visible for a short
  grace period so the death is not a mystery;
- the live window is `min(expires_at, kill_at)` — one predicate, `plan.dies_at`, feeding
  `plan.effective_status`.

### Reaping (there is no core reaper in Phase 1)

sshim notices its own expiries:

- a **throttled sweep** on the browser blueprint's `before_request` — at most once every
  `service.SWEEP_INTERVAL_SECONDS` (30s) per process, so a burst of page loads costs one `UPDATE`;
- a **forced reap** for an operator or cron: `flask sshim-reap` (reports `killed=N expired=M`).

The sweep is **idempotent and concurrency-safe**: the kill-date arm is a single conditional `UPDATE`
(`... WHERE kill_at <= now AND status <> 'killed'`), and the TTL arm writes nothing at all because
`expired` is derived, never stored. Running it twice — or on two workers at once — cannot double-kill or
rewrite an expiry.
