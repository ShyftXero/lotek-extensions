# Methodology reference — framing prose per assessment type

Scribble groups findings into report sections by assessment type (`scribble_assessment_types`, seeded
in `scribble/seed/loader.py`). This skill's job is prose, not scoring — but the RIGHT prose for a
section depends on which of these the section's `type_slug` is, because a non-technical reader expects
a different threat framing for each. Match the narrative to the section's actual assessment type; don't
reuse one section's framing for another.

## `internal`

Internal assessments simulate an attacker who already has a foothold on the internal network — a
compromised workstation, a malicious insider, a dropped implant from a phishing pretext elsewhere. The
narrative frame is **lateral movement and blast radius**: what could this attacker reach, escalate to,
or pivot through once already inside the perimeter. Findings here should read in terms of "an attacker
already on this network could…", not "an internet-facing attacker could…".

## `external`

External assessments simulate an anonymous attacker on the open internet with no prior access — the
perimeter itself. The narrative frame is **initial access**: what is reachable, unauthenticated, from
outside, and what does exploiting it hand the attacker as a first foothold. Findings here should read in
terms of "an internet-facing attacker could…", and should connect cleanly into an `internal` section
later in the same report where the external foothold becomes the internal starting point.

## `web-app`

Web application assessments target a specific application's logic and trust boundaries rather than
network-level exposure — authentication/authorization flaws, injection, business-logic abuse, client-
side trust issues. The narrative frame is **what a legitimate-looking request can be made to do**: what
a normal user of the application, or an unauthenticated visitor to it, can coerce it into doing that its
designers didn't intend. Keep this distinct from `external` framing — a web app finding is about the
application's own logic, not "the network is reachable".

## `device-mobile`

Device/mobile assessments cover a physical device or its companion mobile application — local storage,
platform permission boundaries, on-device secrets, inter-process/inter-app communication, physical
tampering. The narrative frame is **what someone holding the device can extract or bypass**: what is
recoverable from the device or app package itself, not what is reachable over the network. Keep the
prose grounded in device-level facts (storage, permissions, binary) rather than borrowing `web-app` or
`external` framing.

## Cross-section narrative

When an engagement report spans more than one of these four types (a "combined" `scope_type`), the
executive summary should tie them together in a single attacker-story arc where the evidence supports
it — e.g. an `external` foothold finding that leads into an `internal` lateral-movement finding — rather
than four disconnected sections. Never invent a chain the sidecar's findings don't actually support;
only narrate connections the underlying findings evidence.
