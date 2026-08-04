"""Reporting: one ``ReportContext`` feeds both the HTML renderer (WS7) and the docx renderer (WS8).

``context.build_report_context`` is the FROZEN CONTRACT both renderers consume. It honors
include/exclude flags and the group/finding ordering (auto-severity vs manual) so board order == report
order.
"""

from scribble.reporting.context import (  # noqa: F401
    ArtifactCtx,
    FindingCtx,
    GroupCtx,
    ReportContext,
    SeverityRollup,
    build_report_context,
)
