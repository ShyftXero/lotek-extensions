# sshim — SSH pivot generator

A disposable **SSH-only pivot**: a poor-man's VPN to a target network over `ssh`, for a quick task.
Two machines on hostile networks rendezvous through an SSH relay using only the resident `ssh` client —
no binary on disk, runs on a borrowed box. When both sides hang up, it's gone. **Not persistent access.**

## How to use it

1. **Pick a rendezvous.** Phase 1: a 3rd-party box *you* control — a VPS or a tailnet node. **sshim
   ships no rendezvous default**: the mint page opens with a blank host field and no fixed port, and
   minting without one fails with "configure a rendezvous host". (An admin can pre-fill a default via
   the `default_relay_host` setting; it is blank out of the box.)
2. **Mint a pivot** (`/sshim/mint`). Choose a topology, confirm the rendezvous, set a TTL. You get two
   command legs and a one-time relay secret. **The secret is shown once and never stored** — paste it at
   the `ssh` prompt (it is never put on the command line, where `ps` could read it).
3. **Run the two legs** — the *anchor* (inside the target network, default Windows `ssh.exe`) and the
   *operator* (you, default Linux `ssh`). Copy each with its Copy button.
4. **Re-fetch anytime** from the running-pivots list (`/sshim/`). The commands are regenerated from the
   stored session, so you can pull them back on a different borrowed box. The secret can't be re-shown
   (it was never stored) — re-mint if you need a fresh one.

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

## Credential model

One random, URL-safe password of **at least 18 visible characters** (`plan.gen_password`, 24 in
practice), shown once, pasted at the `ssh` prompt, valid for one session. No key generation: `ssh -i`
needs a key file on the borrowed box, which defeats the point of a throwaway pivot.
