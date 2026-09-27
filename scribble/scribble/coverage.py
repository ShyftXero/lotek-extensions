"""scribble/coverage.py — the scan-coverage gate on the way into a client deliverable (lotek#656).

The 2026-09-04 AD-demo post-mortem: six jobs ran against a lab that was never up (``vboxnet0`` DOWN,
both VMs aborted) and every one of them reported ``completed / findings=0``. Nothing distinguished
*we looked and found nothing* from *we never looked*, so absence of evidence was promoted into a
client report as evidence of absence. The operator found out mid-demo.

Core now answers the question instead of collapsing it (``host_contract.JobDTO``, lotek PR #927):

    ``assessed is True``   some module produced tool evidence — the scan genuinely ran
    ``assessed is False``  every module was measured and produced none — nothing was actually tested
    ``assessed is None``   coverage was never measured (a job predating ``job_module_runs.evidence_bytes``)

Core deliberately stops there: ``make_mark_job_promoted`` does not read the verdict, because core
never interprets a promotion. Interpreting it is the extension's job, and this module is where the
extension does it — ONE chokepoint every promote surface goes through, so the rule cannot drift
between the machine API and any browser surface added later (``tests/test_promote_coverage_guard.py``
pins that there is no second, unguarded path).

Two rules, both deliberate:

1. **Only ``True`` promotes.** ``None`` refuses exactly as loudly as ``False``. ``None`` means *we do
   not know*, and "we do not know" is the state that produced the incident — a report that cannot say
   whether it tested anything is the defect, not a lesser version of it. Treating ``None`` as
   promotable would reintroduce the whole class.

2. **The refusal is overridable, but only on purpose.** An operator who knows the coverage is
   inconclusive and wants the findings anyway sends ``acknowledge_inconclusive: true``. It is never a
   default and never inferred. And an acknowledged promotion is not a silent one: it writes a
   coverage note INTO the engagement naming the modules that produced nothing, so the deliverable
   states its own limits rather than keeping them between the operator and the log.
"""

from __future__ import annotations

from typing import Any

from scribble.enums import Confidence, Severity
from scribble.models import BoardFinding

# The title of the note an acknowledged promotion writes. Also the dedup key: re-acknowledging the
# same job into the same engagement refreshes that one row rather than stacking another copy of it
# (promote itself is idempotent -- a note that was not would make a retried call look like new
# coverage problems). ``{job_id}`` is interpolated, so one engagement fed by three partially-assessed
# jobs carries three distinct notes and a reader can tell which scan each one is about.
COVERAGE_NOTE_TITLE = "Scan coverage limitation — job {job_id}"

# Wire/`error` code for the refusal. 409 Conflict, not 400: the request is well-formed and the caller
# is authorized; the JOB is in a state that may not cross into a deliverable. A 400 would read as
# "you typed it wrong" and invite a retry with the same job.
REFUSAL_CODE = "job_not_assessed"
REFUSAL_STATUS = 409


class CoverageRefused(Exception):
    """A promote path refused an unassessed job, raised where the DECISION is made so the ANSWER can be
    the caller's.

    ``engagement_ui._adopt_job_onto_board`` is the one body behind two routes that must answer
    differently: ``adopt_job`` replies to `board.js` with a JSON ``error`` code it branches on, while
    ``adopt_job_by_core`` is a form POST whose operator reads a sentence. Raising instead of
    ``abort``-ing keeps the rule in one place and leaves only the wording to each route. Carries the job
    and id so either can rebuild its own body via ``refusal_body``/``refusal_message``.

    Mirrors ``promote.CrossEngagementPromote``, the refusal idiom this file's callers already handle.
    """

    def __init__(self, job: Any, job_id: str) -> None:
        self.job = job
        self.job_id = job_id
        super().__init__(refusal_message(job, job_id))


# The override's accepted vocabulary. Deliberately the SAME words `api_pat._include_in_report_or_400`
# accepts, because both flags answer the same kind of question (does this reach the client?) and an
# operator should not have to remember two dialects. Kept here rather than imported from `api_pat` so
# the browser surface does not have to import the machine blueprint to agree with it.
_TRUE_WORDS = frozenset({"1", "true", "yes", "on"})
_FALSE_WORDS = frozenset({"0", "false", "no", "off"})


def parse_acknowledgement(raw: Any) -> tuple[bool, bool]:
    """``(acknowledged, well_formed)`` for the override flag. Absent/empty is a well-formed NO.

    Strict, and shared by every promote surface. ``bool(raw)`` is the wrong parse twice over: it reads
    the string ``"false"`` as True, and it would accept ``"maybe"`` as consent. This flag is the only
    thing standing between an unassessed scan and a client report, so anything not recognisably a yes
    or a no comes back ``well_formed=False`` for the caller to refuse -- never a guess. An operator
    overriding a coverage refusal has to have actually said so.
    """
    if raw is None or raw == "":
        return False, True
    if isinstance(raw, bool):
        return raw, True
    if isinstance(raw, str):
        word = raw.strip().lower()
        if word in _TRUE_WORDS:
            return True, True
        if word in _FALSE_WORDS:
            return False, True
    return False, False


def job_assessed(job: Any) -> bool | None:
    """``JobDTO.assessed`` for ``job`` — ``None`` when the host cannot answer at all.

    ``getattr`` with a ``None`` default, NOT ``job.assessed``, and the default is load-bearing: a host
    pinned to a core that predates PR #927 has no such attribute, and that host knows even less about
    its coverage than one answering ``None``. Reading it as missing-therefore-fine would hand exactly
    those deployments the pre-#656 behaviour while the tests here stayed green. Missing collapses into
    ``None`` — *we do not know* — and refuses.
    """
    return getattr(job, "assessed", None)


def unassessed_modules(job: Any) -> tuple[str, ...]:
    """``JobDTO.unassessed_modules`` — every module whose own verdict is not ``True``, pipeline order.

    Empty is a legitimate answer with real meaning, and it is the shape a ``None`` verdict takes: a job
    whose coverage was never measured has no per-module verdicts to list either. So callers must render
    "which modules" and "whether we know" as two separate facts -- see ``coverage_note_body``.
    """
    return tuple(getattr(job, "unassessed_modules", ()) or ())


def may_promote(job: Any) -> bool:
    """Whether ``job`` may cross into a client deliverable un-acknowledged.

    ``is True``, not truthiness: the whole point is that ``None`` and ``False`` are treated alike, and
    ``if job.assessed`` would already do that -- but it would ALSO accept any non-bool truthy value a
    future host might put there (a string ``"partial"``, a count). Identity against ``True`` accepts
    exactly the one verdict that means "a module produced evidence" and nothing that merely resembles it.
    """
    return job_assessed(job) is True


def refusal_body(job: Any, job_id: str) -> dict[str, Any]:
    """The 409 payload. Says WHICH state refused and names the modules, so the caller can act.

    A bare "refused" would push the operator straight to the override, which is the one response this
    guard exists to make deliberate. Handing back the verdict and the module list means the honest next
    step (go fix the scan) is as available as the acknowledged one.
    """
    assessed = job_assessed(job)
    modules = unassessed_modules(job)
    detail = (
        "this job produced no tool evidence — every module ran and measured nothing"
        if assessed is False
        else "this job's scan coverage was never measured, so whether anything was tested is unknown"
    )
    return {
        "error": REFUSAL_CODE,
        "detail": (
            f"refusing to promote job {job_id} into a client deliverable: {detail}. "
            "Re-send with acknowledge_inconclusive=true to promote anyway; the engagement will "
            "record a coverage note naming the unassessed modules."
        ),
        "assessed": assessed,
        "unassessed_modules": list(modules),
    }


def refusal_message(job: Any, job_id: str) -> str:
    """The same refusal as prose, for the browser surface's ``abort(409, ...)``.

    The UI has no flash channel (every refusal in ``engagement_ui`` is an ``abort`` with a sentence),
    so this is what the operator actually reads. It names the modules for the same reason
    ``refusal_body`` does: the useful next step is fixing the scan, not overriding the guard.
    """
    modules = unassessed_modules(job)
    if job_assessed(job) is False:
        why = "it produced no tool evidence — every module ran and measured nothing"
    else:
        why = "its scan coverage was never measured, so whether anything was tested is unknown"
    listed = f" Modules with no evidence: {', '.join(modules)}." if modules else ""
    return (
        f"Refusing to promote scan job {job_id} into this engagement: {why}.{listed} "
        "Re-submit with 'acknowledge inconclusive coverage' to promote anyway — the engagement will "
        "record a coverage note naming the unassessed modules."
    )


def coverage_note_body(job: Any, job_id: str) -> str:
    """The prose the note carries. Names the modules when the host knows them, and says so when not."""
    assessed = job_assessed(job)
    modules = unassessed_modules(job)
    if assessed is False:
        opening = (
            f"Scan job {job_id} was promoted into this engagement after an operator acknowledged that "
            "it produced no tool evidence. Every module in the pipeline ran and was measured; none "
            "returned output to assess."
        )
    else:
        opening = (
            f"Scan job {job_id} was promoted into this engagement after an operator acknowledged that "
            "its scan coverage was never measured. Whether any module actually examined the targets is "
            "unknown for this job."
        )
    if modules:
        listed = ", ".join(modules)
        body = f"Modules that produced no evidence, in pipeline order: {listed}."
    else:
        # Not a formatting fallback -- an unmeasured job HAS no per-module verdicts, so saying "none"
        # here would read as "every module was fine", the exact inversion this whole issue is about.
        body = (
            "No per-module coverage verdicts are available for this job, so the specific modules "
            "cannot be listed."
        )
    closing = (
        "Findings below are therefore not evidence that the in-scope systems are free of the issues "
        "these modules would have detected."
    )
    return f"{opening}\n\n{body}\n\n{closing}"


def write_coverage_note(
    db: Any, *, engagement: Any, job: Any, job_id: str, actor_username: str | None
) -> BoardFinding:
    """Record the acknowledged coverage gap on ``engagement``, returning the row.

    Shaped as a ``BoardFinding`` at ``Severity.info`` rather than as a new engagement-level
    column or a ``VariableValue``, for one reason: it has to be LOUD BY DEFAULT. A column needs a
    template that reads it and a variable needs a template that references ``{{TOKEN}}`` -- under both,
    a deployment whose report template predates this change renders a deliverable that says nothing
    about its own coverage, which is precisely the silence #656 is about. Findings render in every
    shipped template, so the note cannot be lost by not opting in.

    ``Severity.info`` marks it as a disclosure rather than a vulnerability. ``status`` is left at the
    model's default instead of being set here, for two reasons that agree: the obvious candidate,
    ``accepted_risk``, would be a lie -- it means a client weighed this and accepted it, and nobody has
    -- and picking any status here would be a second derivation of what a status MEANS, which
    ``tests/test_report_disposition_single_source.py`` sweeps for and (correctly) flagged when this
    function first tried it. The default leaves the note in the LIVE disposition, so ``report_visible``
    keeps it in the deliverable, which is the whole point of writing it.

    Flat (``group_id=None``, ``parent_id`` unset) and ``source_finding_id=None`` -- it descends from no
    scan finding. Idempotent on (engagement, job): re-acknowledging refreshes the row in place, since
    ``promote_job`` itself is idempotent and a note that stacked would make one retried call look like
    a worsening coverage picture.

    🔴 That idempotence is SEQUENTIAL-retry idempotence, and deliberately no stronger. The lookup below
    reads ``engagement.findings`` and then inserts, so two CONCURRENT acknowledged promotions can both
    see no matching row and write two notes -- ``scribble_findings`` carries no uniqueness that would
    stop them (``source_finding_id`` is indexed ``unique=False``; the model has no ``__table_args__``).

    Not fixed here, because fixing it HERE would be the wrong shape: ``promote.promote_job`` dedups by
    the same read-then-write (it reads ``promoted_source_ids``/``legacy_titles`` into Python sets, then
    inserts), so the same race duplicates every promoted FINDING, not just this note. Giving the note a
    database-enforced key while the findings it annotates keep none would buy a tidy caveat attached to a
    doubled report -- and would publish a one-note-per-(engagement, job) guarantee this module cannot
    honour alone. The race is real and is filed as its own defect (ext#257); it predates #656 and wants
    one constraint covering the promotion as a whole.
    """
    title = COVERAGE_NOTE_TITLE.format(job_id=job_id)
    from scribble.content.schema import doc_from_text  # local: avoids a content <-> models import cycle

    content = {"description": doc_from_text(coverage_note_body(job, job_id))}

    existing = next((f for f in engagement.findings if f.title == title and f.group_id is None), None)
    if existing is not None:
        existing.content_json = content
        return existing

    note = BoardFinding(
        engagement_id=engagement.id,
        group_id=None,
        title=title,
        category="Scan coverage",
        severity=Severity.info,
        confidence=Confidence.high,  # we are certain about the GAP, whatever we lack about the scan
        content_json=content,
        created_by=actor_username,
        order_index=len([f for f in engagement.findings if f.group_id is None]),
    )
    db.add(note)
    return note
