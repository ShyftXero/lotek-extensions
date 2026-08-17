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
never commit. :func:`delete_finding` returns the on-disk artifact paths instead of unlinking them, so the
caller deletes files only AFTER its transaction commits (a rolled-back delete must not leave the bytes
gone — the same order ``engagement_ui.delete_finding``/``engagement_delete`` already used).
"""

from __future__ import annotations

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


def delete_finding(db, finding: EngagementFinding) -> list[str]:
    """Delete a finding AND its artifact rows; returns the artifacts' ``storage_path``s for the caller to
    unlink after it commits.

    Unlike :func:`delete_group`, a finding IS its content: deleting it must take its evidence with it.
    ``EngagementFinding.artifacts`` carries no delete/delete-orphan cascade and ``Artifact.finding_id`` is
    nullable, so a bare ``db.delete(finding)`` would silently NULL out each artifact's ``finding_id``
    (orphaning the row and leaking the file) instead of removing it. The rows go explicitly, here.
    """
    storage_paths = [a.storage_path for a in finding.artifacts]
    for artifact in list(finding.artifacts):
        db.delete(artifact)
    db.delete(finding)
    return storage_paths
