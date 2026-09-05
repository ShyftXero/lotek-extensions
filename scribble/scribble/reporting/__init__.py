"""Reporting: one ``ReportContext`` feeds both the HTML renderer (WS7) and the docx renderer (WS8).

``context.build_report_context`` is the FROZEN CONTRACT both renderers consume. It honors
include/exclude flags and the group/finding ordering (auto-severity vs manual) so board order == report
order.
"""

from scribble.reporting.context import (  # noqa: F401
    DIAGRAM_CAPTION_FALLBACK,
    ArtifactCtx,
    ChainCtx,
    ChainStepCtx,
    DiagramCtx,
    FindingCtx,
    GroupCtx,
    ReportContext,
    RetestCloseoutRow,
    SeverityRollup,
    build_report_context,
    figure_anchor,
    figure_caption,
    figure_label,
    number_figures,
)
