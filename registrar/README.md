# Registrar

Offensive-security **infrastructure inventory + control** for lotek — a mountable Flask extension.
Tracks servers (static/transient), domains, and DNS used during engagements, **provider-agnostic** via a
minimal driver abstraction. Mounts into lotek under `/registrar`, or runs standalone.

## v2-native

- **UUIDv7 PKs**; `engagement_id` is a **UUID soft reference** to a core `Engagement`, no cross-schema FK.
- **No authorization data** — tenancy is core's. Read rule: transient servers are **engagement-scoped**
  (via the host `visible_engagement_ids` seam); static (org) infra is **admin-only** (B2c).
- Tables `registrar_*`: `registrar_servers`, `registrar_domains`, `registrar_dns_records`,
  `registrar_audit`.

## Drivers (minimal, like scribble/vector)

`ComputeDriver` / `DnsDriver` / `SmsDriver` ABCs. The MVP ships one **in-memory `null` backend** per axis
so the gate/audit machinery runs with **no egress and no credentials**; real backends (libcloud
compute+DNS, twilio SMS) implement the same ABCs (`pip install .[providers]`).

## Action-safety gate

**Effect-locality tiers** (not reversibility): any verb whose effect is observable outside this instance —
`create_node`, `destroy_node`, `register_domain`, `upsert_record`, `send_sms` — is **confirm-tier** and
never agent-autonomous. Reads are `direct`. The host resolves a verb's tier and **fails closed** (an
unclassified verb is confirm-tier). Every outcome is **audited** with a **secret-free projection** (an SMS
body/recipient never lands in `registrar_audit`).

Owed hardening (tracked in `INVARIANTS.md`, proposed): a full server-side staged-action state machine
(confirmer ≠ initiator, re-auth at execution, interactive-transport check — INV-EXT-02), and the PAT/agent
read+stage-only surface.

Staged into lotek via `scripts/stage-extension.sh <this-repo> registrar registrar /registrar`.
