"""Demo dataset for screenshots and local dev (not seeded in normal boot).

Creates one client + one combined engagement with a few assessment-type groups and findings
instantiated from the seeded template library. Idempotent: re-running returns the existing demo
engagement instead of duplicating it. Used by ``scripts/capture-screenshots.py`` and available for a
``--demo`` dev flag.
"""

from __future__ import annotations

from sqlalchemy import select

from scribble.models import (
    AssessmentType,
    BoardFinding,
    Client,
    FindingGroup,
    ReportBoard,
    VulnerabilityTemplate,
)

DEMO_ENGAGEMENT = "Acme Q3 Assessment"


def seed_demo(session) -> ReportBoard:
    existing = session.scalar(select(ReportBoard).where(ReportBoard.name == DEMO_ENGAGEMENT))
    if existing is not None:
        return existing

    client = session.scalar(select(Client).where(Client.name == "Acme Corp")) or Client(name="Acme Corp")
    session.add(client)
    session.flush()  # need client.id before assigning it to ReportBoard.client_id (soft reference)
    engagement = ReportBoard(
        name=DEMO_ENGAGEMENT,
        # Deliberately Scribble's OWN Client (not scribble.deps.client_model()): this is fake demo/dev
        # content for screenshots, always meant to land in scribble_clients, never in a mounted host's
        # real client table -- see docs/LOTEK_ADOPTION.md §3.1 / scribble.models.ReportBoard.client_id.
        client_id=client.id,
        company_name="Acme Corporation",
        scope_type="combined",
    )
    session.add(engagement)

    types = {t.slug: t for t in session.scalars(select(AssessmentType)).all()}
    templates = list(session.scalars(select(VulnerabilityTemplate).limit(6)))
    ti = 0
    for order, slug in enumerate(("internal", "external", "web-app")):
        at = types.get(slug)
        group = FindingGroup(
            engagement=engagement,
            name=at.name if at else slug.title(),
            assessment_type_id=at.id if at else None,
            order_index=order,
        )
        session.add(group)
        for j in range(2):
            if ti >= len(templates):
                break
            tmpl = templates[ti]
            ti += 1
            session.add(
                BoardFinding.from_template(
                    tmpl,
                    engagement=engagement,
                    group=group,
                    order_index=j,
                    target_host=f"10.0.0.{ti + 1}",
                    target_url="https://app.acme.test",
                )
            )
    session.flush()
    return engagement
