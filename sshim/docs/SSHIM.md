# sshim — SSH pivot generator

A disposable **SSH-only pivot**: a poor-man's VPN to a target network over `ssh`, for a quick task.
Two machines on hostile networks rendezvous through an SSH relay using only the resident `ssh` client —
no binary on disk, runs on a borrowed box. When both sides hang up, it's gone. **Not persistent access.**

## How to use it

1. **Pick a rendezvous.** Phase 1: a 3rd-party box *you* control — a VPS, a tailnet node, or the
   `shyft.us` CNAME. The mint page opens to `shyft.us:443` by default (admin-settable).
2. **Mint a pivot** (`/sshim/mint`). Choose a topology, confirm the rendezvous, set a TTL. You get two
   command legs and a one-time relay secret. **The secret is shown once and never stored** — paste it at
   the `ssh` prompt (it is never put on the command line, where `ps` could read it).
3. **Run the two legs** — the *anchor* (inside the target network, default Windows `ssh.exe`) and the
   *traveler* (you, default Linux `ssh`). Copy each with its Copy button.
4. **Re-fetch anytime** from the running-pivots list (`/sshim/`). The commands are regenerated from the
   stored session, so you can pull them back on a different borrowed box. The secret can't be re-shown
   (it was never stored) — re-mint if you need a fresh one.

## Topologies

| | mode | when | DNS |
|-|------|------|-----|
| **A** | relay + reverse SOCKS5 | both sides behind NAT | **remote** (anchor resolves) — free |
| **B** | direct SOCKS | anchor is routable (public IP / tailnet) | local (traveler) — *not* remote |
| **C** | relay + sshuttle | Linux traveler wants full-subnet L3 | remote; needs sudo + Python on anchor |

## What it is NOT

SSH-only. No shell, no staged payload, no bind/reverse shell, no MSFVenom/meterpreter. sshim builds
`ssh` forward commands and nothing else.

## Residue

`ssh` writes nothing to `known_hosts` (`UserKnownHostsFile=/dev/null`|`NUL`), accepts the host key on
first use, and forwards loopback-only. The one residue on a borrowed box is **shell history** — the UI
says so and offers the `powershell -EncodedCommand` renderer to keep the cleartext out of it.

## Phase 2 (not built)

A profile will later be able to request a **lotek-hosted ephemeral relay** instead of an operator box,
through the `ephemeral_relay_acquire`/`release` host seam (lotek#1747). Phase 1 works entirely without it.
