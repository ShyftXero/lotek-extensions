"""Engagement-view tenancy: the host-delegated ``can_view_client`` check + the blueprint-wide gate.

Two things live here, deliberately together (one is the primitive, one is what makes the primitive
actually apply everywhere):

1. :func:`authorize_engagement_view` — the single tenancy predicate. Originally written for (and still
   used directly by) the report routes (``report_html_api.py``/``report_docx_api.py``, audit CRIT-4:
   the report + its export embed a client's findings and evidence). Moved out of ``report_html_api.py``
   into its own module so the blueprint-wide gate below can import it without every other route module
   reaching into a report-rendering file for an authorization primitive.

2. :func:`register_gate` — a fail-closed ``before_request`` hook attached to ``bp`` AND ``api_bp`` (NOT
   ``machine_bp``, which authenticates PAT requests through its own, separate ``host.authenticate`` gate
   and has its own tenancy story — see its module docstring). Before this gate existed, the report routes
   were the ONLY callers of :func:`authorize_engagement_view`: every other engagement-scoped route
   (``engagement_ui.py``'s board/edit/delete/add-finding/reorder, ``artifacts_api.py``'s artifact CRUD,
   ``checklists_api.py``'s assignment routes, ``autosave_api.py``/``collab/*``'s per-block routes, …) did
   a bare ``db.get(...)`` with no tenancy check at all — any authenticated actor could read AND write
   another client's engagement data by walking ids. This gate closes that systemically, in one place,
   rather than by remembering to call the primitive at the top of every view function (the very thing
   that went wrong here: the primitive existed and worked, most call sites just never called it).

Resolution is driven by ``request.view_args`` — the route's OWN URL converters, which is why this can be
generic instead of a per-endpoint allowlist: any route whose view args carry one of the recognized id
names below is automatically covered, with zero additional wiring, the day it's added. A route with NO
recognized id name (dashboard/list/create, or a route scoped to a library-wide table that isn't
engagement data at all — ``VulnerabilityTemplate``, ``ChecklistTemplate``, ``AssessmentType``) is not
engagement-scoped by construction and passes straight through.

FAIL CLOSED: if a request's view args name a recognized id but the row (or its engagement) can't be
resolved, this aborts 404 -- the same "a non-member must not distinguish forbidden from nonexistent"
posture :func:`authorize_engagement_view` already carries, extended to "the id doesn't even resolve".

Two known, deliberate gaps this gate does NOT close (view-arg resolution structurally can't reach them;
see the two routes' own inline fixes instead):
  * ``artifacts_api.create_artifact`` (``POST /artifacts``) takes its target ``engagement_id`` from the
    request BODY, not the URL -- it calls :func:`authorize_engagement_view` directly instead.
  * ``templating_api.templating_preview`` (``POST /preview``) is the same shape (body-supplied
    ``engagement_id``) -- same direct-call fix.
"""

from __future__ import annotations

from flask import abort, request

from scribble.deps import current_actor, get_config, open_session
from scribble.models import (
    Artifact,
    Engagement,
    EngagementChecklist,
    EngagementChecklistItem,
    EngagementFinding,
    FindingGroup,
)


def authorize_engagement_view(engagement: Engagement) -> None:
    """Audit CRIT-4 (originally): a client's findings/evidence must not be readable or writable by a
    reader/writer the host would not grant that client to.

    **This function no longer decides anything. It asks the host.** It used to carry a hand-copy of
    lotek's predicate — *"mirroring lotek app/access.py user_can_view_job: admins see everything; a
    non-admin sees only engagements it OWNS"*. Every clause of that sentence became false when the host
    moved to per-engagement memberships: there is no admin bypass any more (an admin holds no implicit
    view of engagement data and must self-grant, audited), and ownership was never the axis. A stale
    copy of an access rule does not merely drift — this one **inverted**, granting every admin full read
    plus the creator a read on a client it may hold no membership under. That is the argument against
    copying a predicate, and the host now exposes ``can_view_client`` so there is nothing left to copy.

    Note the trap that makes the copy so easy to write: Scribble's ``Engagement.owner_id`` is
    ATTRIBUTION only (engagements are team-shared — see the model), whereas the host's ``Job.owner_id``
    used to be the gate. The host has now inverted its own column to match Scribble's meaning. Neither
    is an authorization key; do not reintroduce either as one.

    Standalone Scribble (no host bundle) has no host authorization model, so nothing is enforced there —
    unchanged. With a host wired, this fails CLOSED (404, never 403: do not confirm that the engagement
    id exists to someone who may not read it).
    """
    cfg = get_config()
    if not cfg.extras.get("host"):
        return  # standalone Scribble — no host authorization model to apply
    can_view_client = cfg.extras.get("can_view_client")
    if can_view_client is None:
        # A host bundle that predates the contract. Refuse rather than fall back to a local rule: the
        # whole point is that this module holds no policy of its own to fall back TO.
        abort(404)
    if not can_view_client(getattr(engagement, "client_id", None), current_actor()):
        abort(404)


# ── the blueprint-wide gate ──────────────────────────────────────────────────────────────────────────

# View-arg names that ARE the engagement id directly. Two names because two route modules independently
# named their converter differently (``engagement_id`` almost everywhere; ``eid`` in
# ``checklists_api.py``'s assignment routes) — both mean the same thing.
_DIRECT_KEYS: tuple[str, ...] = ("engagement_id", "eid")


def _via_finding(db, value) -> Engagement | None:
    finding = db.get(EngagementFinding, value)
    return None if finding is None else db.get(Engagement, finding.engagement_id)


def _via_group(db, value) -> Engagement | None:
    group = db.get(FindingGroup, value)
    return None if group is None else db.get(Engagement, group.engagement_id)


def _via_artifact(db, value) -> Engagement | None:
    artifact = db.get(Artifact, value)
    return None if artifact is None else db.get(Engagement, artifact.engagement_id)


def _via_engagement_checklist(db, value) -> Engagement | None:
    checklist = db.get(EngagementChecklist, value)
    return None if checklist is None else db.get(Engagement, checklist.engagement_id)


def _via_checklist_item(db, value) -> Engagement | None:
    item = db.get(EngagementChecklistItem, value)
    if item is None:
        return None
    checklist = item.checklist  # relationship lazy-load, within the same open session
    return None if checklist is None else db.get(Engagement, checklist.engagement_id)


# child-id view-arg name -> (db, id_value) -> Engagement | None. Derived directly from the routes mapped
# in plans/fix-scribble-tenancy-gate.md: every ``bp``/``api_bp`` route whose URL carries a child id that
# belongs to exactly one engagement. NOT a guess -- extending this table is how a brand-new child-id
# route gets covered; ``tests/test_scribble_tenancy_gate.py`` fails closed on any route whose view args
# match neither this table nor the non-scoped allowlist, so a new route can't silently slip through
# unclassified either way.
_CHILD_RESOLVERS: dict[str, object] = {
    "finding_id": _via_finding,
    "group_id": _via_group,
    "artifact_id": _via_artifact,
    "cid": _via_engagement_checklist,
    "iid": _via_checklist_item,
}


def resolve_engagement(db, view_args: dict) -> tuple[bool, Engagement | None]:
    """Resolve the engagement a request's view args target, using the id-name map above.

    Returns ``(is_scoped, engagement)``:
      * ``(False, None)``       — no recognized engagement-identifying view arg; NOT engagement-scoped
                                   by this route's URL shape (dashboard/list/create, or a library-wide
                                   route with no engagement axis at all).
      * ``(True, None)``        — a recognized id was present but did not resolve to a real row (missing
                                   engagement, missing child, or a child with no engagement) — the
                                   caller must fail closed.
      * ``(True, Engagement)``  — resolved; the caller authorizes against this engagement.

    ``engagement_id``/``eid`` win over a child id when a route's URL happens to carry both (e.g.
    ``delete_group``'s ``/engagements/<engagement_id>/groups/<group_id>/delete``) — the direct id is the
    more specific signal, and the route's own logic already separately guards against a child id that
    belongs to a DIFFERENT engagement than the one named in the URL (see e.g. ``delete_group``/
    ``delete_finding``'s ``if child.engagement_id != engagement_id: abort(404)``), which is a distinct
    concern from tenancy and stays exactly where it is.
    """
    for key in _DIRECT_KEYS:
        if key in view_args:
            return True, db.get(Engagement, view_args[key])
    for key, resolver in _CHILD_RESOLVERS.items():
        if key in view_args:
            return True, resolver(db, view_args[key])
    return False, None


def _gate() -> None:
    with open_session() as db:
        is_scoped, engagement = resolve_engagement(db, request.view_args or {})
        if not is_scoped:
            return
        if engagement is None:
            abort(404)  # a recognized id that doesn't resolve — no existence oracle either
        authorize_engagement_view(engagement)


def register_gate(api_bp, bp) -> None:
    """Attach the fail-closed tenancy gate to both cookie-authed blueprints.

    Idempotent by construction the same way every other ``register(api_bp, bp)`` hook in this package
    is: called exactly once, from ``scribble/__init__.py::_wire_feature_routes``, guarded by that
    function's own ``_FEATURE_ROUTES_WIRED`` flag, before the first ``app.register_blueprint`` call.
    """
    bp.before_request(_gate)
    api_bp.before_request(_gate)
