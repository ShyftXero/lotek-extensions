"""Human-facing UI blueprint — the infrastructure inventory (servers + domains + recent audit)."""

from __future__ import annotations

from flask import Blueprint, render_template
from sqlalchemy import select

from registrar._version import __version__
from registrar.deps import (
    current_actor_is_admin,
    get_config,
    host_can_write,
    host_visible_engagement_ids,
)
from registrar.models import AuditRecord
from registrar.service import visible_domains, visible_servers

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
        # Audit is org-wide and un-scopable by engagement (AuditRecord has no engagement_id), so — like
        # the API surfaces (INV-TENANCY-06) — it is admin-only; a non-admin dashboard shows none.
        audit = [
            {"at": a.at.strftime("%Y-%m-%d %H:%M"), "verb": a.verb, "tier": a.tier, "result": a.result}
            for a in (
                db.scalars(select(AuditRecord).order_by(AuditRecord.at.desc()).limit(10)).all()
                if is_admin else []
            )
        ]
    return render_template("registrar/list.html", servers=servers, domains=domains, audit=audit)
