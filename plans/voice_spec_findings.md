# Voice spec: pentest finding writeups

This governs Description, Impact, Replication Steps, and Recommendation prose across the scribble vulnerability library. One template done right is more useful than 263 done fast.

---

## Persona and tense

The assessment team is the author. Third person, past tense, for what the team observed and did. Imperative for remediation. Present tense for Impact — it describes the vulnerability class, not a past event.

| Section | Voice | Tense |
|---|---|---|
| Description | Assessment team, third person | Past |
| Impact | Vulnerability class, third person | Present |
| Replication Steps | Assessment team or reader | Past or imperative |
| Recommendation | Reader, second person | Imperative |

---

## Opening the Description

Start with the specific finding — the host, the service, the affected parameter, the observed behavior. The vulnerability class belongs in Impact, not the opening line.

**Not this:** "SSL/TLS is a widely used protocol for securing communications. Insecure configurations can expose organizations to significant risk."

**This:** "The assessment team identified TLS 1.0 and TLS 1.1 support on {{COMPANY_NAME}}'s external-facing services at {host}:{port}. Both protocols are deprecated (RFC 8996); cipher suites tied to them include RC4 and CBC modes with known plaintext recovery attacks."

If the team exploited the finding, lead with the outcome. "The team authenticated to {service} as an administrator using the factory-default password, then accessed {resource}." The reader needs the severity of the result before the mechanism.

---

## Impact: name the attack, not the risk rating

State what an attacker can do. Name the attack technique. Name the data or access at stake. Severity is in the metadata — do not adjective your way through it in prose.

**Not this:** "This vulnerability poses a significant risk and could potentially allow an attacker to gain unauthorized access to sensitive information."

**This:** "An attacker in a man-in-the-middle position can force a TLS 1.0 session and apply the BEAST attack to recover session tokens from CBC-encrypted traffic. No server compromise is required — network adjacency is enough."

One to three sentences. Mechanism first. Consequence second.

---

## Replication Steps

Specific and reproducible. Name the tool and the key flags. Include the request or command output that confirms the finding — the reader should be able to run the same thing and get the same result. Leave single-brace placeholders for values the author fills in per engagement.

---

## Recommendation

Imperative and prioritized. The highest-impact fix first. Name the exact option and its target value, not the general category.

**Not this:** "It is recommended that {{COMPANY_NAME}} review its TLS configuration and consider disabling legacy protocols where feasible."

**This:** "Disable TLS 1.0 and TLS 1.1 on all public-facing services. Restrict cipher suites to TLS 1.2 AEAD suites (AES-128-GCM, AES-256-GCM, CHACHA20-POLY1305), or migrate to TLS 1.3 exclusively."

Do not open the Recommendation with "the assessment team recommends the following actions:" — start the first list item directly with the action verb.

---

## Variants / affected technologies

If a finding applies differently across technologies or environments, add a `<strong>Variants</strong>` block at the end of the Description section, before the `# Impact` marker. One to two sentences per variant: what's different and why it matters.

---

## Sentence rhythm

Short declaratives. One idea per sentence. After a longer clause, follow with a shorter one. Avoid subordinate chains. No tricolon unless each element earns its place.

---

## Variable usage

Scribble Jinja-renders `{{ }}` tokens at report generation. Use them whenever the concept matches one of the nine canonical variables below. Single-brace `{x}` is for author fill-ins — anything specific that has no matching variable.

**Rule:** prefer `{{VARIABLE}}` when the concept matches; single-brace only when it doesn't. Unknown `{{X}}` tokens blank the block at render.

| Concept | Token |
|---|---|
| Client / company / organization name | `{{COMPANY_NAME}}` |
| Target host, server, hostname, or IP | `{{TARGET_HOST}}` |
| Affected or target URL (including endpoint URL) | `{{TARGET_URL}}` |
| Domain name | `{{DOMAIN}}` |
| Affected items / assets list | `{{AFFECTED}}` |
| Count of affected items | `{{AFFECTED_COUNT}}` |
| Usernames / account names | `{{ACCOUNTS}}` |
| Files / shares / resources / objects | `{{OBJECTS}}` |
| Maximum severity | `{{MAX_SEVERITY}}` |

Single-brace fill-ins (no matching variable): port number, parameter name, application name, tool output, specific payload, configuration value, threshold count, access level, service version.

## Placeholders

- `{{VARIABLE}}` — Jinja-rendered; use one of the nine tokens above when the concept matches
- `{single_brace}` — author fill-in for anything without a matching variable: port, parameter name, app name, payload, specific value
- No other `{{ }}` tokens — scribble blanks unknown Jinja blocks at render time

---

## Words to cut

| Cut | Replace with |
|---|---|
| leverage | use, exploit, abuse |
| robust | name the property |
| significant, serious, severe | name the consequence |
| it should be noted | delete — just say it |
| it is recommended that X consider | imperative verb |
| ensure that | the imperative verb directly |
| mitigate | remediate, disable, restrict, patch |
| best practices | name the practice |
| seamless | cut |
| this vulnerability poses a risk | state the risk |

---

## The register to hit

Think of a senior consultant writing up notes at the end of the day. The prose is fast and exact. It names the mechanism, quotes the tool, states the confirmed result.

If a sentence rates the finding without describing it, cut the rating and describe it. If a recommendation says "consider" or "where feasible," it is not a recommendation — it is a hedge. Either state the fix or say why the fix depends on context.
