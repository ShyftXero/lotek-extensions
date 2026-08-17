"""Board mutation algorithms — the ONE copy, shared by the cookie UI and the PAT machine API.

Extracted from ``engagement_ui.py`` when the machine API grew findings CRUD (ext#41). Before that, the
board's ordering/grouping logic existed only inside cookie view functions defined *inside*
``engagement_ui.register()``, so a machine route could not call it and would have had to re-implement it.
Two implementations of "where does this finding land, and what do the neighbours' ``order_index`` values
become" is precisely the drift that makes a report render in an order nobody chose — and the ordering
rules here are subtle enough (see :func:`place_finding`) that a second copy would not stay identical.

What lives here: the state changes. What does NOT: request parsing, error envelopes, authorization. The
two surfaces have genuinely different contracts for those (``jsonify(error=…)`` + the blueprint-wide
``authz`` gate on the cookie side; ``{"error","detail"}`` + a per-route ``can_view_engagement`` on the
machine side), and collapsing them would mean one surface silently inheriting the other's refusal shape.
So every function here assumes its caller has already ANSWERED "may this actor touch this engagement?"
and validated the input — nothing in this module authorizes anything.

Session handling is the caller's too: these functions mutate ORM objects in the caller's open session and
never commit (:func:`detach_children` flushes, which is not a commit — see its docstring for why the order
matters). :func:`delete_finding` hands back the on-disk artifact paths instead of unlinking them, so the
caller deletes files only AFTER its transaction commits (a rolled-back delete must not leave the bytes
gone — the same order ``engagement_ui.delete_finding``/``engagement_delete`` already used).
"""

from __future__ import annotations

from typing import NamedTuple

from sqlalchemy import select, update

from scribble.enums import OrderMode, severity_rank
from scribble.models import Engagement, EngagementFinding, FindingGroup


def _coerce_int(value) -> int | None:
    """Tolerant int parse for an id inside a client-supplied ORDER LIST. ``None`` for anything
    unparseable, so a stale/garbage entry in a reorder payload is skipped rather than 500ing — the
    defensive posture ``reorder_groups`` documents."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def display_order(findings, order_mode: OrderMode) -> list[EngagementFinding]:
    """Board display order for one group's findings.

    Mirrors ``reporting/context.py::_order_findings`` (auto_severity = worst-first then order_index;
    manual = order_index only) but WITHOUT the ``include_in_report`` filter — authors need to see (and
    re-include) excluded findings on the board, unlike the rendered report.
    """
    items = list(findings)
    if order_mode == OrderMode.manual:
        return sorted(items, key=lambda f: f.order_index)
    return sorted(items, key=lambda f: (severity_rank(f.severity), f.order_index))


def ungrouped_display_order(engagement: Engagement, *, exclude_id: int | None = None) -> list:
    """The ungrouped bucket in the order the board shows it: severity-first, then ``order_index``.

    The bucket has no ``order_mode`` of its own (it is not a ``FindingGroup``), which is exactly why this
    is a named function rather than an inline ``sorted`` — an ``auto_severity`` default guessed at a call
    site is how the two surfaces would diverge on the one collection that has no mode to read.
    """
    return sorted(
        (f for f in engagement.findings if f.group_id is None and f.id != exclude_id),
        key=lambda f: (severity_rank(f.severity), f.order_index),
    )


def nested_child_ids(findings) -> set[int]:
    """The ids in ``findings`` that the RENDERER nests under a parent instead of rendering top-level.

    ONE copy of the nesting rule, shared with ``reporting/context.py::_nest_findings`` (which calls this)
    and with :func:`rendered_top_level_count`. A finding counts as nested only when its ``parent_id``
    resolves to another finding IN THIS SAME list that is itself a true parent (``parent_id is None``) —
    one level of nesting, no queries. A child whose parent is missing from the list (excluded from the
    report, or sitting in a different bucket) renders top-level, so it is NOT nested here either.
    """
    by_id = {f.id: f for f in findings}
    nested: set[int] = set()
    for finding in findings:
        if finding.parent_id is None:
            continue
        parent = by_id.get(finding.parent_id)
        if parent is not None and parent.parent_id is None:
            nested.add(finding.id)
    return nested


def rendered_top_level_count(engagement: Engagement) -> int:
    """How many findings the REPORT renders at TOP level — the number to quote as "N findings".

    A flat count of ``engagement.findings`` is NOT that number: promotion produces a parent per vuln type
    with the per-host instances as CHILDREN, and the renderer nests those inside their parent's card, so a
    caller counting board rows over-reports (a 1-parent/2-child cluster is one finding in the deliverable,
    three on the board). Excluded groups and ``include_in_report=False`` findings drop out too, exactly as
    ``reporting/context.py::build_report_context`` drops them.

    Mirrors that function's bucket walk rather than re-deriving the rule — and
    ``test_top_level_count_matches_what_the_renderer_produces`` asserts equality against the renderer's own
    output, so the two cannot drift silently.
    """
    buckets = [
        [f for f in group.findings if f.include_in_report]
        for group in engagement.groups
        if group.include_in_report
    ]
    buckets.append([f for f in engagement.findings if f.group_id is None and f.include_in_report])
    return sum(len(bucket) - len(nested_child_ids(bucket)) for bucket in buckets)


def reindex(items) -> None:
    """Assign sequential order_index (0..n-1) to items in their current list order. The single place
    that decides on-disk order_index values for a reorder/move — no gaps, no duplicates."""
    for index, item in enumerate(items):
        item.order_index = index


def place_finding(
    finding: EngagementFinding, target_group: FindingGroup | None, requested_index: int
) -> FindingGroup | None:
    """Move ``finding`` into ``target_group`` (``None`` = the ungrouped bucket) at ``requested_index``.

    Returns the finding's PREVIOUS group when the move changed groups, else ``None`` — the caller needs
    that to report which two groups it touched.

    Caller's obligations, none of which this function re-checks: ``target_group`` belongs to the SAME
    engagement as ``finding`` (a cross-engagement move is a tenancy break, refused by both surfaces
    before they get here), and the actor may write this engagement.

    The ordering rule is the subtle part, and it is the reason this is shared code rather than two
    copies. Both affected lists are computed BEFORE ``finding`` is mutated, so the reads reflect the
    pre-move state unambiguously, independent of session autoflush timing. And the caller's
    ``order_index`` is a slot in the RENDERED order — the browser board reads DOM position, and for an
    ``auto_severity`` group the board renders severity-ranked, NOT order_index-ranked. So the destination
    siblings must be arranged the SAME way the board displayed them before inserting at the requested
    index; otherwise the finding lands among the wrong neighbours and the reindex below freezes an order
    the user never chose (board order != document order).

    Any move into a group flips it to ``OrderMode.manual`` — a drag IS a deliberate manual placement.
    "Re-rank by severity" (setting ``order_mode`` back to ``auto_severity``) is the explicit way back.
    """
    old_group = finding.group
    old_group_id = old_group.id if old_group is not None else None
    new_group_id = target_group.id if target_group is not None else None
    same_group = old_group_id == new_group_id

    if target_group is not None:
        dest_siblings = display_order(
            [f for f in target_group.findings if f.id != finding.id], target_group.order_mode
        )
    else:
        dest_siblings = ungrouped_display_order(finding.engagement, exclude_id=finding.id)

    index = max(0, min(requested_index, len(dest_siblings)))
    dest_siblings.insert(index, finding)

    remaining = None
    if not same_group and old_group is not None:
        remaining = sorted(
            (f for f in old_group.findings if f.id != finding.id), key=lambda f: f.order_index
        )

    reindex(dest_siblings)
    if remaining is not None:
        reindex(remaining)

    # Assign the RELATIONSHIP, not the ``group_id`` column. Setting the FK alone leaves the ORM's
    # in-memory graph stale: ``FindingGroup.findings`` is an already-loaded collection and SQLAlchemy only
    # syncs a backref when the relationship attribute is set, so the destination group would not contain
    # the finding until the session expired it.
    #
    # That is invisible for ONE move per session (which is all the cookie board ever does) and wrong for
    # several: the bulk machine move places findings in a loop, and with the FK assignment every finding
    # after the first computed ``dest_siblings`` from a collection that still looked empty, so they all
    # landed at index 0 and the caller's order was silently discarded. Caught by
    # ``test_bulk_move_preserves_the_listed_order``; recorded here because the FK version looks correct and
    # passes every single-move test.
    finding.group = target_group
    # …and the column too, deliberately. The relationship assignment above only writes ``group_id`` when
    # the session FLUSHES, so a caller that reads ``finding.group_id`` to build its response (both move
    # routes do) would otherwise report the PREVIOUS group. Setting both is redundant at flush time — they
    # agree — and it keeps the object correct to read immediately, without every caller remembering to
    # flush first.
    finding.group_id = new_group_id
    if target_group is not None:
        target_group.order_mode = OrderMode.manual

    return None if same_group else old_group


def reorder_groups(engagement: Engagement, requested_order) -> list[int]:
    """Apply a client-supplied group order to ``engagement``; returns the ids in their new order.

    Defensive by design: an id that is unparseable, duplicated, or belongs to another engagement is
    ignored, and anything belonging to this engagement that the client did NOT mention (a stale/partial
    payload — e.g. a group created concurrently in another tab) keeps its relative order and is appended
    after the ones explicitly placed. So no group is ever silently dropped or left with an undefined
    position, and a stale payload is not a 500.
    """
    existing = {g.id: g for g in engagement.groups}
    ordered_ids: list[int] = []
    seen: set[int] = set()
    for raw_id in requested_order:
        gid = _coerce_int(raw_id)
        if gid is not None and gid in existing and gid not in seen:
            ordered_ids.append(gid)
            seen.add(gid)

    leftover = sorted(
        (g for g in existing.values() if g.id not in seen), key=lambda g: g.order_index
    )
    ordered_ids.extend(g.id for g in leftover)

    for index, gid in enumerate(ordered_ids):
        existing[gid].order_index = index
    return ordered_ids


def create_group(db, engagement: Engagement, *, name: str, assessment_type=None) -> FindingGroup:
    """Append a new ``FindingGroup`` to ``engagement`` (last position). Flushed, not committed, so the
    caller can read the assigned PK and still roll the whole request back."""
    group = FindingGroup(
        engagement_id=engagement.id,
        assessment_type=assessment_type,
        name=name,
        order_index=len(engagement.groups),
    )
    db.add(group)
    db.flush()
    return group


def delete_group(db, group: FindingGroup) -> None:
    """Delete a group, DETACHING its findings (``group_id`` -> NULL) rather than deleting them.

    Removing a report *section* must never silently destroy authored findings — the board is a two-level
    tree, not a hierarchy of ownership over content. Contrast :func:`delete_finding`.
    """
    for finding in list(group.findings):
        finding.group_id = None
    db.delete(group)


def detach_children(db, finding: EngagementFinding) -> list[int]:
    """NULL the ``parent_id`` of every finding nested under ``finding``; returns their ids in board order.

    Nothing else clears that column. ``EngagementFinding.parent_id`` is a self-FK with **no** ``ondelete``
    and **no** ORM relationship (the model keeps the self-ref config deliberately trivial), so neither the
    database nor the unit of work detaches a child for us: deleting a parent while a child still points at
    it raises ``IntegrityError`` on SQLite and ``ForeignKeyViolation`` on Postgres, and the whole
    transaction dies. That is the DEFAULT shape, not a corner case — ``promote.promote_job`` creates a
    parent per matched vuln template with every per-host instance as a child, so a promoted finding was
    precisely the thing a delete was most likely to be handed.

    Children are DETACHED, never deleted, for :func:`delete_group`'s reason: the parent is a synthesized
    umbrella row built from the vuln-DB template, while the CHILD is the row carrying the irreplaceable
    per-host evidence (its own ``target_host``, ``variables``, ``source_finding_id`` and artifacts). One
    DELETE must not destroy N per-host findings the caller never named. Detached children keep their group
    and ``order_index``, so they stay where the board already showed them and simply render top-level —
    the shape ``reporting/context.py::_nest_findings`` already handles for an unresolvable parent.

    Flushed here on purpose: the UPDATEs must reach the database BEFORE the caller's ``db.delete(finding)``
    issues its DELETE, and relying on the unit of work's internal save-before-delete ordering for that
    would make an FK violation an implementation detail away.
    """
    children = db.scalars(
        select(EngagementFinding)
        .where(EngagementFinding.parent_id == finding.id)
        .order_by(EngagementFinding.order_index, EngagementFinding.id)
    ).all()
    for child in children:
        child.parent_id = None
    if children:
        db.flush()
    return [child.id for child in children]


def flatten_nesting(db, engagement: Engagement) -> None:
    """Clear every ``parent_id`` in ``engagement`` so a cascade delete of the whole engagement is
    ordering-independent.

    ``Engagement.findings`` cascades ``delete-orphan``, and with no ORM relationship on the self-FK (see
    :func:`detach_children`) SQLAlchemy has no dependency to sort those DELETEs by — it emits them in one
    executemany batch and the child rows' ``parent_id`` FK fails. So deleting an engagement that holds ANY
    promoted aggregation raised ``IntegrityError``/``ForeignKeyViolation``: the engagement could not be
    deleted at all. (Found while fixing :func:`detach_children`, not reported — a pre-existing defect of
    the same class on the cookie ``engagement_delete`` route.)

    A Core UPDATE, not per-object assignment: the findings are about to be marked deleted, and the unit of
    work does not emit an UPDATE for an object it is deleting — so the assignment would be dropped and the
    FK would fail anyway.
    """
    db.execute(
        update(EngagementFinding)
        .where(
            EngagementFinding.engagement_id == engagement.id,
            EngagementFinding.parent_id.isnot(None),
        )
        .values(parent_id=None)
    )


class DeletedFinding(NamedTuple):
    """What :func:`delete_finding` leaves for its CALLER to finish.

    ``storage_paths`` are unlinked only after the caller commits; ``detached_child_ids`` are reported back
    to the caller so a delete that changed OTHER rows says so (the machine route puts them in its response
    body — a per-host child silently becoming a top-level finding would otherwise be invisible).
    """

    storage_paths: list[str]
    detached_child_ids: list[int]


def delete_finding(db, finding: EngagementFinding) -> DeletedFinding:
    """Delete a finding AND its artifact rows, DETACHING any children; returns the artifacts'
    ``storage_path``s for the caller to unlink after it commits, plus the detached children's ids.

    Unlike :func:`delete_group`, a finding IS its content: deleting it must take its evidence with it.
    ``EngagementFinding.artifacts`` carries no delete/delete-orphan cascade and ``Artifact.finding_id`` is
    nullable, so a bare ``db.delete(finding)`` would silently NULL out each artifact's ``finding_id``
    (orphaning the row and leaking the file) instead of removing it. The rows go explicitly, here.

    Nested CHILDREN are the opposite case and go the other way — see :func:`detach_children` for why they
    survive, and for why omitting that step made this an outright 500 on any promoted finding.
    """
    storage_paths = [a.storage_path for a in finding.artifacts]
    for artifact in list(finding.artifacts):
        db.delete(artifact)
    detached_child_ids = detach_children(db, finding)
    db.delete(finding)
    return DeletedFinding(storage_paths, detached_child_ids)
