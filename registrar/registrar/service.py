"""Registrar domain operations — the action-safety gate, staged-action state machine, audit, read rule.

Kept free of Flask so it is unit-testable. Invariants enforced here:

- **Tier gate (INV-EXT-03).** A ``confirm``-tier verb (outward effect) never executes inline — it is
  STAGED (a ``registrar_staged_actions`` row). An unclassified verb is confirm-tier (fail closed).
- **Server-side staged approval (INV-EXT-02).** A staged action executes ONLY from ``approve``, which
  requires an INTERACTIVE session (not a PAT/agent), a confirmer DIFFERENT from the initiator, and a
  live write authorization — checked here, server-side, not in the UI.
- **Audit is redacted + dual-written (INV-SECRET-05 / INV-AUDIT-03).** The local ``registrar_audit`` row
  carries a secret-free projection; the same call also appends to CORE ``audit_events`` via the host
  ``audit`` seam (``ext:registrar:*``), in the same transaction as the change.
- **Read rule (B2c).** Transient servers are engagement-scoped; static (org) infra is admin-only.
"""

from __future__ import annotations

import json
import uuid  # noqa: F401  (used in type hints / callers)
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from registrar.drivers import get_compute, get_dns, get_sms, tier_of
from registrar.enums import ServerKind, ServerState, Tier
from registrar.models import AuditRecord, Domain, Server, StagedAction


class ConfirmationRequired(Exception):
    """A confirm-tier verb reached execution without a valid interactive approval."""


class ApprovalDenied(PermissionError):
    """A staged action was approved by an ineligible principal (non-interactive / same-user / unauth)."""


# Allow-listed, secret-free projection of each verb's args for the audit detail (INV-SECRET-05).
def _audit_detail(verb: str, args: dict) -> str:
    if verb == "send_sms":
        return "to=<redacted> body=<redacted>"  # never store the recipient or the message
    if verb in ("create_node", "destroy_node"):
        return f"name={str(args.get('name', ''))[:64]}"
    if verb in ("upsert_record", "register_domain"):
        return f"domain={str(args.get('domain', args.get('name', '')))[:120]}"
    return ""


def record_audit(db: Session, *, verb: str, provider: str, tier: Tier, actor: str | None,
                 result: str, args: dict, host_audit=None, subject_id=None) -> None:
    """Write the local (UI) audit row AND — when mounted — the core audit_events row via the host seam.
    Both land in the SAME transaction as the change (the caller commits). Neither carries raw args."""
    db.add(AuditRecord(
        at=datetime.now(UTC), actor=actor, verb=verb, provider=provider, tier=tier.value,
        detail=_audit_detail(verb, args), result=result,
    ))
    if host_audit is not None:
        host_audit(db, f"ext:registrar:{result}", subject_type="registrar_action",
                   subject_id=subject_id, after={"verb": verb, "provider": provider, "tier": tier.value})


@dataclass
class ActionResult:
    status: str          # executed | staged
    detail: dict


def _dispatch(verb: str, provider: str, args: dict) -> dict:
    if verb in ("list_nodes", "create_node", "destroy_node"):
        d = get_compute(provider)
        if verb == "list_nodes":
            return {"nodes": [n.__dict__ for n in d.list_nodes()]}
        if verb == "create_node":
            return {"node": d.create_node(name=args.get("name", "node")).__dict__}
        return {"destroyed": d.destroy_node(args.get("provider_ref", ""))}
    if verb in ("list_records", "upsert_record", "register_domain"):
        d = get_dns(provider)
        if verb == "list_records":
            return {"records": d.list_records(args.get("domain", ""))}
        if verb == "upsert_record":
            return {"record": d.upsert_record(args.get("domain", ""), rtype=args.get("rtype", "A"),
                                              name=args.get("name", "@"), value=args.get("value", ""))}
        return {"domain": d.register_domain(args.get("name", ""))}
    if verb == "send_sms":
        return {"sms": get_sms(provider).send_sms(to=args.get("to", ""), body=args.get("body", ""))}
    raise ValueError(f"unknown verb: {verb}")


def execute_direct(db: Session, *, verb: str, provider: str = "null", args: dict | None = None,
                   actor: str | None = None, host_audit=None) -> ActionResult:
    """Run a DIRECT-tier (read/internal) verb inline. A confirm-tier verb must be staged, not run here."""
    args = args or {}
    if tier_of(verb) is not Tier.direct:
        raise ConfirmationRequired(f"verb '{verb}' is confirm-tier; it must be staged, not executed inline")
    result = _dispatch(verb, provider, args)
    record_audit(db, verb=verb, provider=provider, tier=Tier.direct, actor=actor, result="executed",
                 args=args, host_audit=host_audit)
    db.commit()
    return ActionResult(status="executed", detail=result)


def stage(db: Session, *, verb: str, provider: str = "null", args: dict | None = None,
          initiator_id: uuid.UUID | None, engagement_id=None, actor: str | None = None,
          host_audit=None) -> StagedAction:
    """Record a confirm-tier action as PENDING. It does NOT execute — approval is a separate, gated step."""
    args = args or {}
    if tier_of(verb) is Tier.direct:
        raise ValueError(f"verb '{verb}' is direct-tier; stage() is for confirm-tier verbs")
    row = StagedAction(verb=verb, provider=provider, args_json=json.dumps(args), initiator_id=initiator_id,
                       engagement_id=engagement_id, status="pending", created_at=datetime.now(UTC))
    db.add(row)
    db.flush()
    record_audit(db, verb=verb, provider=provider, tier=Tier.confirm, actor=actor, result="staged",
                 args=args, host_audit=host_audit, subject_id=row.id)
    db.commit()
    return row


def approve(db: Session, staged: StagedAction, *, confirmer_id: uuid.UUID | None,
            confirmer_name: str | None, is_interactive: bool, can_write: bool,
            can_operate_on=None, host_audit=None) -> ActionResult:
    """Execute a staged action — the ONLY path that runs a confirm-tier verb (INV-EXT-02). Server-side:
    an interactive session, a confirmer different from the initiator, live write authz, AND — for an
    engagement-bound action — the confirmer's own operator capability on that engagement re-checked at
    execution time (never inherited from the initiator)."""
    if staged.status != "pending":
        raise ConfirmationRequired(f"staged action is already {staged.status}")
    if not is_interactive:
        raise ApprovalDenied("approval requires an interactive dashboard session, not a machine/PAT caller")
    if confirmer_id is None or confirmer_id == staged.initiator_id:
        raise ApprovalDenied("the confirmer must be a different user than the initiator")
    if not can_write:
        raise ApprovalDenied("not authorized to approve")
    eng = staged.engagement_id
    if eng is not None and can_operate_on is not None and not can_operate_on(eng):
        raise ApprovalDenied("the confirmer is not an operator on the action's engagement")
    args = json.loads(staged.args_json or "{}")
    result = _dispatch(staged.verb, staged.provider, args)
    staged.status = "executed"
    staged.confirmed_by = confirmer_id
    staged.confirmed_at = datetime.now(UTC)
    record_audit(db, verb=staged.verb, provider=staged.provider, tier=Tier.confirm, actor=confirmer_name,
                 result="executed", args=args, host_audit=host_audit, subject_id=staged.id)
    db.commit()
    return ActionResult(status="executed", detail=result)


def reject(db: Session, staged: StagedAction, *, rejector_id: uuid.UUID | None,
           rejector_name: str | None, is_interactive: bool, can_write: bool,
           can_operate_on=None, host_audit=None) -> ActionResult:
    """Reject a staged confirm-tier action WITHOUT executing it. Same interactive + write + engagement-
    operator gates as :func:`approve`, MINUS the confirmer-different-from-initiator rule: rejecting runs
    nothing, so it is safe for the initiator to cancel their own pending action. Sets ``rejected`` and
    audits (result=``rejected``), so a declined action is as attributable as an executed one."""
    if staged.status != "pending":
        raise ConfirmationRequired(f"staged action is already {staged.status}")
    if not is_interactive:
        raise ApprovalDenied("rejection requires an interactive dashboard session, not a machine/PAT caller")
    if not can_write:
        raise ApprovalDenied("not authorized to reject")
    eng = staged.engagement_id
    if eng is not None and can_operate_on is not None and not can_operate_on(eng):
        raise ApprovalDenied("the rejector is not an operator on the action's engagement")
    staged.status = "rejected"
    staged.confirmed_by = rejector_id
    staged.confirmed_at = datetime.now(UTC)
    record_audit(db, verb=staged.verb, provider=staged.provider, tier=Tier.confirm, actor=rejector_name,
                 result="rejected", args=json.loads(staged.args_json or "{}"), host_audit=host_audit,
                 subject_id=staged.id)
    db.commit()
    return ActionResult(status="rejected", detail="staged action rejected")


def visible_servers(db: Session, *, visible_engagement_ids, is_admin: bool) -> list[Server]:
    """Transient servers scoped to the actor's engagements; static (org) infra is admin-only (B2c)."""
    out = []
    for s in db.scalars(select(Server)).all():
        if s.kind is ServerKind.static:
            if is_admin or visible_engagement_ids is None:
                out.append(s)
        elif visible_engagement_ids is None or (s.engagement_id in visible_engagement_ids):
            out.append(s)
    return out


def _scope_included(bound_engagement_id, *, visible_engagement_ids, is_admin: bool) -> bool:
    """The single read-scope rule shared by every registrar list (INV-TENANCY-06), mirroring
    ``visible_servers``:

    * ``visible_engagement_ids is None`` -> standalone / no host scoping -> everything is visible.
    * a row bound to an engagement is visible iff that engagement is in the caller's visible set.
    * an UNBOUND row (no engagement) is org-level inventory, like a ``static`` server -> admin-only.
    """
    if visible_engagement_ids is None:
        return True
    if bound_engagement_id is None:
        return is_admin
    return bound_engagement_id in visible_engagement_ids


def visible_domains(db: Session, *, visible_engagement_ids, is_admin: bool) -> list[Domain]:
    """Domains scoped to the actor's engagements by checkout (INV-TENANCY-06). A domain checked out to
    an engagement is visible only to a caller who can see that engagement; an available domain
    (``checked_out_to is None``) is org inventory -> admin-only. Standalone sees all."""
    return [
        d for d in db.scalars(select(Domain).order_by(Domain.name)).all()
        if _scope_included(d.checked_out_to, visible_engagement_ids=visible_engagement_ids,
                           is_admin=is_admin)
    ]


def visible_staged(db: Session, *, visible_engagement_ids, is_admin: bool) -> list[StagedAction]:
    """Pending staged actions scoped to the actor's engagements (INV-TENANCY-06). An engagement-bound
    staged action is visible only to a caller who can see that engagement; an unbound one is org-level
    -> admin-only. Standalone sees all."""
    rows = db.scalars(
        select(StagedAction).where(StagedAction.status == "pending")
        .order_by(StagedAction.created_at.desc())
    ).all()
    return [
        s for s in rows
        if _scope_included(s.engagement_id, visible_engagement_ids=visible_engagement_ids,
                           is_admin=is_admin)
    ]


def stage_from_reconcile(db: Session, provider: str = "null") -> int:
    """Import live provider state and flag drift LOUDLY (INV-DEPLOY-03): a transient server the provider
    no longer reports is marked ``missing`` (never silently dropped, never terminalized on an unreachable
    provider). Returns the count newly marked missing. MVP: the null backend reports nothing gone."""
    live = {n.provider_ref for n in get_compute(provider).list_nodes()}
    marked = 0
    for s in db.scalars(select(Server)).all():
        if (s.provider == provider and s.provider_ref and s.provider_ref not in live
                and s.kind is ServerKind.transient and s.state is not ServerState.missing):
            s.state = ServerState.missing
            marked += 1
    db.commit()
    return marked
