"""Standing-prose defaults + override dedup (ext#346 item 3) — the ONE home for the Methodology and
Scope/limitations editable overrides on ``ReportBoard``.

Two jobs, one module so they can't drift:

1. **Defaults** — the generated standing text the editor is PREFILLED with, so an operator overrides from
   the real baseline instead of an empty box. Sourced from the same renderer constants the report uses
   (``reporting.render_html``), so the prefilled text and the rendered standing text are the same words.
2. **Dedup** — a value is stored ONLY on a SUBSTANTIAL diff from that default. We normalize whitespace
   first, so a single trailing space (or the operator saving the prefilled default untouched) never writes
   a duplicate override row that just echoes the standing text. NULL means "use the generated default".
"""

from __future__ import annotations

import re


def _normalize_prose(text: str | None) -> str:
    """Collapse all runs of whitespace to one space and strip the ends — the canonical form two prose
    values are compared in, so cosmetic whitespace can't make them 'differ'."""
    return re.sub(r"\s+", " ", text or "").strip()


def prose_differs_substantially(submitted: str | None, default: str) -> bool:
    """True when ``submitted`` is a real edit of ``default`` rather than the same text modulo whitespace."""
    return _normalize_prose(submitted) != _normalize_prose(default)


def override_for_storage(submitted: str | None, default: str) -> str | None:
    """What to persist for a prose-override field: ``None`` (fall back to the generated default) when the
    box is empty OR differs only trivially from the default; otherwise the submitted text, stripped."""
    submitted = (submitted or "").strip()
    if not submitted or not prose_differs_substantially(submitted, default):
        return None
    return submitted


def methodology_default_text() -> str:
    """The generated standing Methodology prose as plain text: the lead paragraph plus one paragraph per
    phase. Matches the report's standing render (``reporting.render_html``), minus the per-section-type
    framing the renderer adds from the engagement's groups (an override replaces the generated prose, so
    the framing is dropped there too)."""
    from scribble.reporting.render_html import _METHODOLOGY_LEAD, _METHODOLOGY_PHASES

    paras = [_METHODOLOGY_LEAD]
    paras += [f"{name}. {text}" for name, text in _METHODOLOGY_PHASES]
    return "\n\n".join(paras)


def scope_limitations_default_text() -> str:
    """The generated standing Scope-and-limitations statement as plain text — the same sentences the report
    renders, one per paragraph."""
    from scribble.reporting.render_html import _LIMITATIONS

    return "\n\n".join(_LIMITATIONS)
