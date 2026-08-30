"""Human-facing UI blueprint — the infrastructure inventory (servers + domains + recent audit)."""

from __future__ import annotations

from flask import Blueprint, abort, redirect, render_template, url_for
from sqlalchemy import select

from registrar._version import __version__
from registrar.deps import (
    current_actor_id,
    current_actor_is_admin,
    current_actor_username,
    get_config,
    host_audit,
    host_can_operate_on,
    host_can_write,
    host_is_interactive,
    host_visible_engagement_ids,
)
from registrar.models import AuditRecord, StagedAction
from registrar.service import (
    ApprovalDenied,
    ConfirmationRequired,
    _scope_included,
    approve,
    reject,
    visible_domains,
    visible_servers,
    visible_staged,
)

bp = Blueprint("registrar", __name__, template_folder="templates")


@bp.context_processor
def _inject_base():
    cfg = get_config()
    return {
        "registrar_base": cfg.base_template,
        "registrar_version": __version__,
        "registrar_can_write": host_can_write(),
        "api_base": cfg.url_prefix.rstrip("/") + "/api",
    }


@bp.get("/")
def dashboard():
    cfg = get_config()
    is_admin = current_actor_is_admin()
    visible = host_visible_engagement_ids()
    with cfg.session_factory() as db:
        servers = [
            {"kind": s.kind.value, "state": s.state.value, "name": s.name, "provider": s.provider,
             "ip": s.ip or "—", "role": s.role or "—"}
            for s in visible_servers(db, visible_engagement_ids=visible, is_admin=is_admin)
        ]
        domains = [
            {"name": d.name, "provider": d.provider, "registered": d.registered}
            for d in visible_domains(db, visible_engagement_ids=visible, is_admin=is_admin)
        ]
        # Pending staged (confirm-tier) actions the actor may see — the human half of the approve
        # workflow (INV-EXT-02). `approvable` is False for the initiator's own rows so the two-person
        # rule is VISIBLE (they see it, cannot approve it) rather than an opaque 403 on click.
        me = current_actor_id()
        staged = [
            {"id": str(s.id), "verb": s.verb, "provider": s.provider,
             "args": s.args_json or "{}",
             "initiator": str(s.initiator_id) if s.initiator_id else "—",
             "at": s.created_at.strftime("%Y-%m-%d %H:%M"),
             "approvable": s.initiator_id is None or s.initiator_id != me}
            for s in visible_staged(db, visible_engagement_ids=visible, is_admin=is_admin)
        ]
        # Audit is org-wide and un-scopable by engagement (AuditRecord has no engagement_id), so — like
        # the API surfaces (INV-TENANCY-06) — it is admin-only; a non-admin dashboard shows none.
        audit = [
            {"at": a.at.strftime("%Y-%m-%d %H:%M"), "verb": a.verb, "tier": a.tier, "result": a.result}
            for a in (
                db.scalars(select(AuditRecord).order_by(AuditRecord.at.desc()).limit(10)).all()
                if is_admin else []
            )
        ]
    return render_template("registrar/list.html", servers=servers, domains=domains, audit=audit,
                           staged=staged, can_write=host_can_write())


def _load_visible_staged_or_404(db, staged_id):
    """Fetch a staged row the current actor is allowed to see, or 404 — mirrors the read scope so a
    staged id from another tenant can't be approved/rejected by guessing it (INV-TENANCY-06)."""
    staged = db.get(StagedAction, staged_id)
    if staged is None or not _scope_included(
        staged.engagement_id, visible_engagement_ids=host_visible_engagement_ids(),
        is_admin=current_actor_is_admin(),
    ):
        abort(404)
    return staged


@bp.post("/staged/<uuid:staged_id>/approve")
def approve_staged_ui(staged_id):
    """Browser (no-JS form) approval. The service re-checks every gate server-side (INV-EXT-02): an
    interactive session, a confirmer different from the initiator, write authz, and the confirmer's own
    operator capability on the action's engagement. PAT callers never reach an interactive session, so
    the deliberate absence of a machine approve route is preserved."""
    cfg = get_config()
    with cfg.session_factory() as db:
        staged = _load_visible_staged_or_404(db, staged_id)
        try:
            approve(db, staged, confirmer_id=current_actor_id(),
                    confirmer_name=current_actor_username(), is_interactive=host_is_interactive(),
                    can_write=host_can_write(), can_operate_on=host_can_operate_on,
                    host_audit=host_audit())
            notice = "Staged action approved and executed."
            return redirect(url_for("registrar.dashboard", notice=notice))
        except (ApprovalDenied, ConfirmationRequired) as e:
            return redirect(url_for("registrar.dashboard", error=str(e)))


@bp.post("/staged/<uuid:staged_id>/reject")
def reject_staged_ui(staged_id):
    """Browser (no-JS form) rejection. Declines a staged action without executing it; audited."""
    cfg = get_config()
    with cfg.session_factory() as db:
        staged = _load_visible_staged_or_404(db, staged_id)
        try:
            reject(db, staged, rejector_id=current_actor_id(),
                   rejector_name=current_actor_username(), is_interactive=host_is_interactive(),
                   can_write=host_can_write(), can_operate_on=host_can_operate_on,
                   host_audit=host_audit())
            return redirect(url_for("registrar.dashboard", notice="Staged action rejected."))
        except (ApprovalDenied, ConfirmationRequired) as e:
            return redirect(url_for("registrar.dashboard", error=str(e)))
