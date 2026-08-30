"""Engagement-view tenancy: the host-delegated ``can_view_client`` check + the blueprint-wide gate.

Two things live here, deliberately together (one is the primitive, one is what makes the primitive
actually apply everywhere):

1. :func:`can_view_client_id` — the single tenancy predicate (and its wrappers
   :func:`can_view_engagement`, :func:`filter_visible_engagements`, and the aborting
   :func:`authorize_engagement_view`). Originally written for (and still used directly by) the report
   routes (``report_html_api.py``/``report_docx_api.py``, audit CRIT-4: the report + its export embed a
   client's findings and evidence). Moved out of ``report_html_api.py`` into its own module so the
   blueprint-wide gate below can import it without every other route module reaching into a
   report-rendering file for an authorization primitive.

   The predicate is exposed in BOTH forms on purpose. ``authorize_engagement_view`` (aborting, implicit
   ``current_actor()``) fits a cookie-authed view that wants to stop right there; the plain predicates
   fit the callers it cannot serve:
     * ``api_pat.py``'s machine routes, whose principal is the PAT actor (``extras['pat_actor']``), not
       the browser-session user — a machine request has no session, so an implicit ``current_actor()``
       would resolve to None and 404 every machine route. The host's ``can_view_client`` is duck-typed
       on ``.id`` precisely so it can take either principal shape.
     * list routes, which must FILTER rather than abort: aborting on the first foreign row would turn a
       dashboard into a 404 for anyone whose database contains another tenant's engagement.
     * the two body-scoped routes below (``create_artifact``/``templating_preview``): they call
       :func:`can_view_engagement` explicitly rather than the aborting wrapper so a foreign-but-real
       engagement id gets the SAME response as a nonexistent one — their OWN JSON 404, not Flask's
       default HTML error page, which the aborting wrapper would produce and which was a minor
       existence oracle (distinguishable body/content-type for the same status code; adversarial
       review on #256).

2. :func:`register_gate` — a fail-closed ``before_request`` hook attached to ``bp`` AND ``api_bp``, and
   deliberately NOT to ``machine_bp``: the gate resolves the actor via ``current_actor()``, which is None
   on a PAT request, so attaching it there would 404 every machine route. ``machine_bp`` calls the same
   predicate with its own principal, per route — see ``api_pat.py``'s SECURITY banner. Before this gate
   existed, the report routes
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
    request BODY, not the URL -- it calls :func:`can_view_engagement` directly instead (not the aborting
    wrapper -- see above for why).
  * ``templating_api.templating_preview`` (``POST /preview``) is the same shape (body-supplied
    ``engagement_id``) -- same direct-call fix.
"""

from __future__ import annotations

from flask import abort, request
from sqlalchemy import false, or_

from scribble.deps import current_actor, get_config, host_can_write, open_session
from scribble.models import (
    Artifact,
    Engagement,
    EngagementChecklist,
    EngagementChecklistItem,
    EngagementDiagram,
    EngagementFinding,
    FindingGroup,
)


def host_is_mounted() -> bool:
    """Whether a host bundle was injected (``cfg.extras['host']``).

    Standalone Scribble has no host authorization model at all, so every tenancy check below degrades to
    "allowed" — the same fail-OPEN-when-unmounted posture the report routes have always had. Callers that
    need to REQUIRE something only meaningful under a host (``api_pat``/``engagement_ui``: an engagement
    must name a client the actor can see) branch on this rather than inferring it from a False predicate,
    which would be indistinguishable from "denied".
    """
    return bool(get_config().extras.get("host"))


def can_view_client_id(client_id, actor) -> bool:
    """*May this actor read data belonging to this client?* — asked of the HOST, never decided here.

    The non-aborting core of :func:`authorize_engagement_view`; see that function's docstring for the
    history (a hand-copied predicate that inverted) and this module's for why both forms exist.

    ``actor`` is passed EXPLICITLY — a browser-session user for a cookie route, a ``PatActor`` for a
    machine route. Nothing here reads anything off it; it is handed straight to the host.

    Fails CLOSED (False) when a host is mounted but exposes no ``can_view_client``: a host bundle that
    predates the contract gets a refusal, not a fallback to a local rule — the whole point is that this
    module holds no policy of its own to fall back TO. Returns True only when there is no host at all.
    """
    cfg = get_config()
    if not cfg.extras.get("host"):
        return True  # standalone Scribble — no host authorization model to apply
    can_view_client = cfg.extras.get("can_view_client")
    if can_view_client is None:
        return False
    return bool(can_view_client(client_id, actor))


def can_view_engagement(engagement: Engagement, actor) -> bool:
    """:func:`can_view_client_id` applied to an engagement's ``client_id``.

    Note the consequence for a CLIENT-LESS engagement (``client_id is None``): the host's contract
    answers False for it, so it is invisible to everyone, its creator included. That is not this
    module's choice and not a bug to work around here — an engagement with no client has no tenancy
    anchor, and inventing one (owner? creator?) is exactly the copied-predicate mistake documented in
    :func:`authorize_engagement_view`. The right fix is upstream, at the two CREATE paths, which now
    refuse to make such an engagement while a host is mounted rather than silently making one nobody can
    ever open (``api_pat.scribble_create_engagement`` / ``engagement_ui.engagement_new``).
    """
    return can_view_client_id(getattr(engagement, "client_id", None), actor)


def host_visible_client_ids():
    """The host's scoped SET of client ids for this request, or ``None`` when there isn't one.

    ``None`` means "no set available" — standalone Scribble (no host), or a host bundle predating the
    hook — and is deliberately distinct from an EMPTY set, which means "this actor holds nothing" and
    must scope everything away. Conflating the two is how a fail-closed check turns into a fail-open one.

    Named after cream's ``host_visible_engagement_ids`` convention, and prefixed ``host_`` so it cannot
    be mistaken for (or collide with) the seam function itself — this module holds no policy, it asks.
    """
    cfg = get_config()
    if not cfg.extras.get("host"):
        return None
    hook = cfg.extras.get("visible_client_ids")
    if hook is None:
        return None
    return frozenset(hook())


def visible_engagements(db, stmt, actor) -> list:
    """Run ``stmt`` (a ``select(Engagement)``) scoped to what ``actor`` may see, preferring SQL.

    Two paths, and the difference is the point of the host's ``visible_client_ids`` hook:

    * **the set is available** — narrow in SQL (``WHERE client_id IN (…)``), so the database returns
      only rows this actor may see. One host call, regardless of how many engagements exist.
    * **it isn't** (standalone, or an older host bundle) — fall back to reading the rows and filtering
      them through the per-client predicate, which is what shipped with the tenancy fix.

    The fallback is why this is a helper rather than an inline ``where``: a route must not have to
    choose, and must not silently skip scoping when the newer hook is missing.
    """
    client_ids = host_visible_client_ids()
    if client_ids is None:
        return filter_visible_engagements(db.scalars(stmt).all(), actor)

    # An empty set is NOT "unscoped": it means this actor holds nothing, so nothing matches.
    scoped = Engagement.client_id.in_(client_ids) if client_ids else false()

    # ``IN (…)`` never matches NULL, so a CLIENT-LESS engagement would be invisible on this path
    # whatever the host thinks — while the predicate path shows it to anyone the host answers True for.
    # The real lotek host answers False for a NULL client (no admin bypass in v2, so nobody sees one),
    # but a host is free to answer otherwise and Scribble's own test host does exactly that for admins.
    # Asking the predicate once for the NULL case keeps the two paths identical for EVERY host instead
    # of only the one this was written against — the "second, drifting copy" this helper exists to avoid.
    if can_view_client_id(None, actor):
        scoped = or_(scoped, Engagement.client_id.is_(None))

    return list(db.scalars(stmt.where(scoped)).all())


def filter_visible_engagements(engagements, actor) -> list:
    """The list-route form: drop every engagement whose client the actor holds no grant under.

    A list route is the third shape of the same tenancy defect and the one with no id to gate on — the
    dashboard and the engagement list enumerated EVERY tenant's engagements (names, client names,
    counts), which needs no id-guessing at all. They can't call an aborting check (one foreign row would
    404 the whole page), so they filter.

    The host answer is cached per distinct ``client_id`` for the duration of the call: the real
    ``can_view_client`` opens a session and resolves memberships on every invocation, and a list is
    typically many engagements over few clients.
    """
    if not host_is_mounted():
        return list(engagements)  # standalone — nothing to scope against
    seen: dict[object, bool] = {}
    visible = []
    for engagement in engagements:
        client_id = getattr(engagement, "client_id", None)
        if client_id not in seen:
            seen[client_id] = can_view_client_id(client_id, actor)
        if seen[client_id]:
            visible.append(engagement)
    return visible


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

    The COOKIE-route form: implicit ``current_actor()`` (the browser session user) + abort. A machine
    route has no session and must pass its PAT actor to :func:`can_view_engagement` explicitly.
    """
    if not can_view_engagement(engagement, current_actor()):
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


def _via_diagram(db, value) -> Engagement | None:
    diagram = db.get(EngagementDiagram, value)
    return None if diagram is None else db.get(Engagement, diagram.engagement_id)


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
    "diagram_id": _via_diagram,
    "cid": _via_engagement_checklist,
    "iid": _via_checklist_item,
}

# Precomputed once at import time -- lets `_gate` check "is this request even POTENTIALLY scoped"
# (a pure dict/set membership check, no DB) before paying for an `open_session()`, since most routes on
# `bp`/`api_bp` are library-wide or dashboard/list/create and never carry any of these names.
_RECOGNIZED_VIEW_ARG_NAMES: frozenset[str] = frozenset(_DIRECT_KEYS) | frozenset(_CHILD_RESOLVERS)


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


#: Non-mutating HTTP methods — the WRITE gate below applies to everything else.
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def _gate() -> None:
    view_args = request.view_args or {}
    if not (view_args.keys() & _RECOGNIZED_VIEW_ARG_NAMES):
        return  # not engagement-scoped by this route's URL shape — skip the DB round trip entirely
    with open_session() as db:
        is_scoped, engagement = resolve_engagement(db, view_args)
        if not is_scoped:
            return  # pragma: no cover - unreachable given the pre-check above; kept for safety
        if engagement is None:
            abort(404)  # a recognized id that doesn't resolve — no existence oracle either
        authorize_engagement_view(engagement)
    # WRITE axis. The view check above only proves the caller may SEE this engagement. Until now EVERY
    # mutating scribble route on a scoped URL — `add_finding`, `create_group`, the `delete_*` routes,
    # `promote_job`, the `api_bp` reorder/move endpoints — relied on that view check plus the UI-only
    # `scribble_can_write` display flag, which hides buttons but does not stop a direct POST. So a caller
    # who could view an engagement (a viewer, or a global operator holding a read-only membership) could
    # mutate its report board by posting the form's URL. This closes that class in ONE place: a mutating
    # request additionally requires write capability, the cookie analogue of the machine surface's
    # `require_pat_scope("write")`. 403, not 404: the caller demonstrably can view the row, so its
    # existence is not a secret to protect here.
    #
    # LIMIT (honest): `host_can_write()` is the GLOBAL write flag the host injects; scribble has no
    # per-engagement operate hook (the host gives it `can_view_client` + `can_write`, not
    # `can_operate_on`), so this cannot distinguish a global operator's operator- vs observer-membership
    # on THIS engagement the way lotek core's `is_operator_on` does. It shuts the viewer-writes hole; a
    # global operator with only an observer membership here is still (narrowly) able to write until a
    # host `can_operate_on` hook exists for scribble. Tracked as a follow-on.
    if request.method not in _SAFE_METHODS and not host_can_write():
        abort(403)


def register_gate(api_bp, bp) -> None:
    """Attach the fail-closed tenancy gate to both cookie-authed blueprints.

    Idempotent by construction the same way every other ``register(api_bp, bp)`` hook in this package
    is: called exactly once, from ``scribble/__init__.py::_wire_feature_routes``, guarded by that
    function's own ``_FEATURE_ROUTES_WIRED`` flag, before the first ``app.register_blueprint`` call.
    """
    bp.before_request(_gate)
    api_bp.before_request(_gate)
