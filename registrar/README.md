# Registrar

Offensive-security **infrastructure inventory + control** for lotek — a mountable Flask extension.
Tracks servers (static/transient), domains, and DNS used during engagements, **provider-agnostic** via a
minimal driver abstraction. Mounts into lotek under `/registrar`, or runs standalone.

**Operator documentation:** [`docs/REGISTRAR.md`](docs/REGISTRAR.md) — UI surface, data model, the machine
(PAT) API, the action-safety gate, and security posture. That doc ships inside the wheel and renders on
lotek's in-app Docs page.

## How lotek consumes it

Registrar is a **pinned git dependency** — lotek installs it and discovers it via the
`lotek.extensions` entry point (`registrar = "registrar"`), which resolves to the module exposing
`register(...)`. It is **not vendored** and **not staged by any script** (the old
`stage-extension.sh` flow is retired). The mount metadata (nav, machine prefix, owned tables, docs) is
carried in `lotek-extension.toml`, shipped inside the wheel so the host reads it straight from the
installed package.

## v2-native

- **UUIDv7 PKs**; `engagement_id` is a **UUID soft reference** to a core `Engagement`, no cross-schema FK.
- **No authorization data** — tenancy is core's. Read rule: transient servers are **engagement-scoped**
  (via the host `visible_engagement_ids` seam); static (org) infra is **admin-only**.
- Tables `registrar_*`: `registrar_servers`, `registrar_domains`, `registrar_dns_records`,
  `registrar_staged_actions`, `registrar_audit`.

## Drivers (minimal, like scribble/vector)

`ComputeDriver` / `DnsDriver` / `SmsDriver` ABCs. The MVP ships one **in-memory `null` backend** per axis
so the gate/audit machinery runs with **no egress and no credentials**; real backends (libcloud
compute+DNS, twilio SMS) implement the same ABCs (`pip install .[providers]`).

## Action-safety gate

**Effect-locality tiers** (not reversibility): any verb whose effect is observable outside this instance —
`create_node`, `destroy_node`, `register_domain`, `upsert_record`, `send_sms` — is **confirm-tier** and
never agent-autonomous. Reads are `direct`. The host resolves a verb's tier and **fails closed** (an
unclassified verb is confirm-tier). A confirm-tier action is **staged** and executes only from an
interactive human approval (confirmer ≠ initiator, re-auth at execution). Every outcome is **audited**
with a **secret-free projection** (an SMS body/recipient never lands in `registrar_audit`).
