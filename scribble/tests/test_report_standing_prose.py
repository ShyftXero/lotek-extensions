"""The standing prose must not assert per-engagement WORK that nobody recorded (adversarial review of
the ext#43 front matter / ext#42 methodology).

Scribble's headline workflow is bulk promotion: ``POST .../promote-job/<job_id>`` turns every finding of a
scan job into report findings in one call. The prose the renderer emits unconditionally therefore has to be
true of a report assembled that way, because it is emitted over the assessor's name, into a client
deliverable, next to a section titled "Compliance Attestation" — and there is no field, flag or template an
operator can use to take it out.

It was not. Before this guard the document asserted, of every engagement:

    "Every candidate weakness was validated by hand before it was reported ... Candidates that did not
     survive validation are not reported."          (_METHODOLOGY_PHASES, "Manual validation")
    "Testing was non-destructive ... anything outside the agreed scope was not touched."  (_LIMITATIONS)

For 40 promoted nessus findings both statements are false, and the second one is emitted from inside
``_render_summary`` — so it could not be dropped without dropping the whole Executive Summary.

The rule this module pins, and the reason it is phrase-level rather than shape-level: standing prose may
describe METHOD (present tense, "this is how an assessment of this kind is conducted") and may state
LIMITATIONS (what the report does not claim — under-claiming is the safe direction). It may NOT assert, in
the past tense, that specific work was performed on this engagement, because the renderer has no way to
know whether it was. Anything in that category belongs in the per-engagement, operator-authored prose field
that is still to be built (see plans/fix-scribble-report-render-sweep.md).

Note the deliberate trap this also covers: the stylesheet ships INSIDE the document, so a forbidden phrase
written into a CSS comment fails here too — which is correct, a client reading the file can see it.
"""

from __future__ import annotations

import pytest

from scribble.content import schema
from scribble.models import Engagement, EngagementFinding, FindingGroup
from scribble.reporting import build_report_context
from scribble.reporting.layouts import list_layouts
from scribble.reporting.render_html import _LIMITATIONS, render_report_html

# Past-tense assertions about work done on THIS engagement. Each one was in the shipped prose.
#
# 🔴 A PHRASE list is a weak instrument and has already been slipped once: the bullet that replaced
# "anything outside the agreed scope was not touched" said the same thing as "Systems, accounts and
# techniques outside them WERE NOT EXAMINED", which no entry here matched, so this module reported green
# over exactly the class of sentence it exists to stop. Both wordings are listed now, but the real control
# is reading the prose — a rewording is always one edit away from getting past a blacklist.
FORBIDDEN = [
    "was validated by hand",
    "Testing was non-destructive",
    "was not touched",
    "were not examined",
    "was not examined",
    "no destructive action was taken",
    "Testing followed",
    "was not observed in this environment",
]


@pytest.fixture
def promoted_report(session_factory):
    """The reviewer's scenario, minimally: findings that arrived by promotion (``source_finding_id`` set),
    with no coverage checklist and no evidence of hand validation anywhere in the record."""
    with session_factory() as db:
        eng = Engagement(name="Bulk Promoted", company_name="Acme", scope_type="external")
        grp = FindingGroup(engagement=eng, name="External", order_index=0)
        for i in range(3):
            EngagementFinding(
                engagement=eng, group=grp, title=f"Nessus plugin {i}", severity="medium",
                order_index=i, source_finding_id=1000 + i, target_host=f"10.0.0.{i}",
                content_json={"description": schema.doc_from_text("Carried over from the scan.")},
            )
        db.add_all([eng, grp])
        db.commit()
        eid = eng.id
    with session_factory() as db:
        return render_report_html(build_report_context(db.get(Engagement, eid)))


@pytest.mark.parametrize("phrase", FORBIDDEN)
def test_the_report_asserts_no_unrecorded_work(promoted_report, phrase):
    assert phrase not in promoted_report, (
        f"the deliverable asserts {phrase!r} for work the renderer cannot know happened — for a "
        "bulk-promoted engagement this is a false statement in a client report"
    )


@pytest.mark.parametrize("layout", [lay.name for lay in list_layouts()])
def test_no_shipped_layout_can_reintroduce_the_claims(session_factory, layout):
    """Every shipped Layout, because "a Layout could drop the block" is not a control here: the Layout
    registry is FROZEN data (reporting/layouts.py) with no editor and no operator route, so all
    of them ship the same prose and the only real fix is for the prose itself to be honest."""
    with session_factory() as db:
        eng = Engagement(name="Bulk Promoted", company_name="Acme", scope_type="external")
        db.add(eng)
        db.commit()
        eid = eng.id
    with session_factory() as db:
        html = render_report_html(build_report_context(db.get(Engagement, eid)), layout=layout)
    for phrase in FORBIDDEN:
        assert phrase not in html, f"layout {layout!r} asserts {phrase!r}"


def test_the_methodology_says_it_is_a_standing_description_not_a_work_log(promoted_report):
    """The load-bearing sentence: without it, present-tense method prose still reads to a client as a
    description of what was done on their engagement. It has to hold whether or not a coverage checklist
    was recorded — the existing "no engagement-specific coverage checklist was recorded" note only appears
    when there is none."""
    assert "standing description of method" in promoted_report
    assert "not a log of what was done on this engagement" in promoted_report


def test_the_limitations_still_say_what_the_report_does_NOT_claim(promoted_report):
    """The other half: removing the unverifiable claims must not gut the section. A limitations statement
    that under-claims is safe and useful; an empty one is neither."""
    assert "Scope and limitations" in promoted_report
    assert "describes the environment as it was during the testing window" in promoted_report
    assert "not proof that none exists" in promoted_report
    assert "bounded by the agreed scope" in promoted_report


def test_the_coverage_bullet_makes_a_CLAIM_about_the_report_not_about_the_testing(promoted_report):
    """The bullet this rule has now caught twice, pinned by its actual wording.

    First it read "Testing was non-destructive … anything outside the agreed scope was not touched"; the
    replacement said the same thing as "Systems, accounts and techniques outside them WERE NOT EXAMINED",
    which the phrase list above did not match, so this module reported green over exactly the sentence it
    exists to stop (second adversarial review, 2026-08-17). Both are assertions about how the engagement
    was conducted, in a document the renderer builds without knowing. The wording that is allowed states
    the same limitation as a property of the REPORT, which is true however the findings got here — and
    asserting the allowed wording (not merely the absence of the bad one) is what stops a revert from
    passing by deleting the bullet."""
    assert "makes no claim about systems, accounts or techniques outside them" in promoted_report
    for bullet in _LIMITATIONS:
        assert "this report" in bullet.lower() or "the absence of a finding" in bullet.lower(), (
            f"limitations bullet {bullet!r} names no subject it is limiting — every bullet here has to be "
            "a statement about what THIS REPORT does or does not claim, which is the property that makes "
            "the block safe to emit unconditionally over the assessor's name"
        )
