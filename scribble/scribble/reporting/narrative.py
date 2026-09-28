"""No-LLM templated prose for the engagement report (LOT-48/4).

The default report path emits every word here with ZERO model calls. Prose is Jinja templates over
structured facts already carried in the ``ReportContext`` -- severity counts, the coverage posture,
the discovered-attack-path count, and each finding's own fields. Same input -> same output, offline,
on a Pi. No branch in this module reaches a model.

Two templated surfaces (LOT-48 plan rev ce55490f, §4 / PR-map step 3):

- ``render_executive_summary`` -- the executive-summary paragraph, keyed on the severity rollup, the
  could-not-run coverage posture, and the count of discovered cradle-to-Domain-Admin attack paths.
  It states what was tested, what was found, and what could not run, IN THAT ORDER, and it never
  reports "no issues" for a job that did not run (plan §5, INV-DATA-08).
- ``render_finding_one_liner`` -- a one-sentence summary of a single finding over its ``FindingCtx``
  fields (severity, exploitability tier, host, CVE), so every finding reads in one voice without a
  human writing each.

An OPTIONAL AI-assist seam (``polish``) can rephrase already-rendered prose for FLOW ONLY. It is OFF
by default, batched, removable, and NEVER in the decision path: it may not add, drop, reorder,
reclassify, or re-severity a single fact. With no provider (the default) it returns its input
byte-for-byte, so the report is identical whether or not a model was ever configured. This mirrors
the ``draft_api`` seam posture: the host's ``ai_stream`` egress gate defaults OFF and is the real
switch, and the deterministic string above is always the source of truth.

The strings below are Ghostwriter's Lotek-voice copy (delivered on LOT-72, honesty-doctrine-correct,
run through unsloppify), wired here by Hardcopy. Ghostwriter's LOT-74 voice pass read every rendered
branch against the voice guide and unsloppify's six failure modes, and tightened three: the multi-CVE
clause no longer stutters "CVEs CVE-...", the could-not-run branch says "the scope we assessed" in the
active voice, and the highest-risk sentence agrees in number with a single finding. Jinja placeholders
map to the structured facts the engine already
carries on ``ReportContext``/``FindingCtx``; where Ghostwriter's copy referenced facts the engine does
not yet carry (named gap_reason / covered_scope / unassessed_scope), the could-not-run branches render on
the coverage-limited-job COUNT instead of naming scope, so no scope prose is fabricated (evidence-first).
House rules honored in every string: no em dashes, no semicolons, no UTF middle dots (all three also fail
``test_report_standing_prose``).
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from jinja2 import Environment

from scribble import host

if TYPE_CHECKING:  # avoid an import cycle with context.py (which imports THIS module)
    from scribble.reporting.context import FindingCtx, SeverityRollup

# Env flag that, when explicitly truthy AND a host AI hook is present, opts the report into the prose
# polish seam. Absent/empty/anything-not-a-yes -> OFF, so the default deployment is fully deterministic.
_AI_POLISH_ENV = "SCRIBBLE_REPORT_AI_POLISH"
# Host capability name for the optional AI hook, matching the draft_api seam convention.
_AI_HOOK = "ai_stream"

# autoescape is intentionally OFF: this module emits PLAIN TEXT that the renderers escape themselves
# (render_html wraps ``ctx.narrative`` in ``_esc(...)``). Escaping here would double-escape "&"/"<".
_ENV = Environment(autoescape=False, trim_blocks=True, lstrip_blocks=True)


# The executive summary. Ghostwriter's four coverage branches (LOT-72 comment): FOUND, RAN-CLEAN,
# COULD-NOT-RUN, MIXED. Order within a branch is fixed: tested -> found -> could-not-run (plan §5). The
# "no issues" (RAN-CLEAN) wording is reachable ONLY when nothing could-not-run, so it can never describe
# an unassessed job (AC2 / INV-DATA-08). Whitespace is collapsed after rendering. ``path_sentence`` and
# ``top_sentence`` are prebuilt fragments (empty when they do not apply) and each already carries its own
# leading space. ``gap_clause`` names the coverage-limited-job count (the engine does not carry named
# scope strings, so the branch renders on the count, never fabricated scope prose).
_EXEC_SUMMARY_SRC = """
{%- if total == 0 and coverage_limited_jobs > 0 -%}
  We could not complete the assessment of {{ company }}. {{ gap_clause }}. This report covers only the
  scope we assessed. No findings here does not mean the rest is clean. It means we did not test it.
{%- elif total == 0 -%}
  We assessed {{ company }} and found no issues in the tested scope. That is a clean result for what we
  tested, not a clean bill for what we did not.
{%- elif coverage_limited_jobs > 0 -%}
  We assessed {{ company }} and found {{ total }} {{ issue_word }}: {{ severity_breakdown }}.
  {{- path_sentence }} We could not assess {{ gap_noun }}. This report makes no claim about that scope.
{%- else -%}
  We assessed {{ company }} and found {{ total }} {{ issue_word }}: {{ severity_breakdown }}.
  {{- path_sentence }}{{ top_sentence }}
{%- endif -%}
"""

# The per-finding one-liner (Ghostwriter's shape): severity word + location + optional CVE clause + an
# optional KEV sentence. No title (the finding card carries its own), one voice for every finding.
# ``location`` always resolves ("the tested scope" as the floor), so the sentence never dangles.
_FINDING_ONE_LINER_SRC = """
{{ severity_word }} severity on {{ location }}{{ cve_clause }}.{{ kev_sentence }}
"""

_EXEC_SUMMARY_TMPL = _ENV.from_string(_EXEC_SUMMARY_SRC)
_FINDING_ONE_LINER_TMPL = _ENV.from_string(_FINDING_ONE_LINER_SRC)


def _collapse_ws(text: str) -> str:
    """Collapse every run of whitespace to a single space and strip the ends -- so the template can be
    written for legibility and still emit one clean paragraph/sentence."""
    return " ".join(text.split())


# Severity bands, most severe first, and the word each renders as in prose (Ghostwriter: info ->
# "informational"). Drives the severity_breakdown and keeps the ordering in one place.
_SEVERITY_BANDS: tuple[tuple[str, str], ...] = (
    ("critical", "critical"),
    ("high", "high"),
    ("medium", "medium"),
    ("low", "low"),
    ("info", "informational"),
)


def _oxford(items: list[str]) -> str:
    """Prose list join: "" / "A" / "A and B" / "A, B, and C". No semicolons (house rule)."""
    items = [i for i in items if i]
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    if len(items) == 2:
        return f"{items[0]} and {items[1]}"
    return ", ".join(items[:-1]) + f", and {items[-1]}"


def _severity_breakdown(rollup: SeverityRollup) -> str:
    """Every NONZERO severity band, most severe first, as "2 critical, 1 high, and 3 low". Reads the
    LIVE-only counts already computed in ``rollup`` (lotek#618), so it never overstates present risk.
    "" only when the rollup is empty (a branch that does not use this fragment)."""
    bits = [f"{rollup.counts.get(key, 0)} {word}" for key, word in _SEVERITY_BANDS
            if rollup.counts.get(key, 0)]
    return _oxford(bits)


def _path_sentence(path_count: int) -> str:
    """The cradle-to-Domain-Admin path sentence, with a leading space, or "" when no path was chained.
    Singular / plural per Ghostwriter's copy. ``path_count`` is ``len(ctx.chains)`` (report-included)."""
    if path_count <= 0:
        return ""
    if path_count == 1:
        return " We chained a full path from a starting foothold to Domain Admin."
    return f" We chained {path_count} separate paths from a starting foothold to Domain Admin."


def _top_sentence(top_titles: list[str]) -> str:
    """The highest-risk-findings sentence, with a leading space, or "" when there is no LIVE crit/high
    finding to name. ``top_titles`` is already LIVE-only, ordered, and capped at 3 by the caller. Number
    agrees with the count: one finding reads "finding is", more read "findings are" (voice: it must not
    say "findings are X" for a single X)."""
    names = [t for t in (top_titles or []) if t]
    prose = _oxford(names)
    if not prose:
        return ""
    if len(names) == 1:
        return f" The highest-risk finding is {prose}."
    return f" The highest-risk findings are {prose}."


def _gap_noun(coverage_limited_jobs: int) -> str:
    """The coverage-limited scan-job count as a noun phrase: "1 scan job" / "N scan jobs". The engine
    carries the COUNT, not named scope, so the could-not-run branches name the count and never fabricate a
    scope description (evidence-first)."""
    return f"{coverage_limited_jobs} scan {'job' if coverage_limited_jobs == 1 else 'jobs'}"


def _gap_clause(coverage_limited_jobs: int) -> str:
    """The standalone could-not-run sentence fragment for the COULD-NOT-RUN branch: "1 scan job could not
    be assessed". Count-based (see ``_gap_noun``)."""
    return f"{_gap_noun(coverage_limited_jobs)} could not be assessed"


def render_executive_summary(
    *,
    company_name: str,
    rollup: SeverityRollup,
    top_titles: list[str],
    path_count: int = 0,
    coverage_limited_jobs: int = 0,
) -> str:
    """The executive-summary paragraph, rendered deterministically from structured facts (no model).

    Ghostwriter's four coverage branches (LOT-72), selected on ``total`` and ``coverage_limited_jobs``:
    FOUND (found, no gap), RAN-CLEAN (nothing found, no gap), COULD-NOT-RUN (nothing found, a gap), and
    MIXED (found, and a gap). The RAN-CLEAN "no issues" wording is reachable only when nothing
    could-not-run, so it can never describe an unassessed job (AC2 / INV-DATA-08).

    ``company_name``           the client/company name, or "" (falls back to a neutral phrase).
    ``rollup``                  the ``SeverityRollup`` (LIVE-only severity counts + total).
    ``top_titles``             worst LIVE finding titles, already ordered and capped by the caller.
    ``path_count``             discovered cradle-to-Domain-Admin attack paths (``len(ctx.chains)``).
    ``coverage_limited_jobs``  scan jobs promoted on an acknowledged coverage gap (plan §5). > 0 ->
                               the summary states the could-not-run posture and NEVER reads "no issues".
    """
    company = company_name or "the target environment"
    total = rollup.total
    coverage_limited_jobs = max(0, int(coverage_limited_jobs or 0))
    rendered = _EXEC_SUMMARY_TMPL.render(
        company=company,
        total=total,
        issue_word=("issue" if total == 1 else "issues"),
        severity_breakdown=_severity_breakdown(rollup),
        path_sentence=_path_sentence(max(0, int(path_count or 0))),
        top_sentence=_top_sentence(list(top_titles or [])),
        gap_clause=_gap_clause(coverage_limited_jobs),
        gap_noun=_gap_noun(coverage_limited_jobs),
        coverage_limited_jobs=coverage_limited_jobs,
    )
    return _collapse_ws(rendered)


# Severity value -> the word Ghostwriter's one-liner uses (info -> "Informational").
_SEVERITY_WORDS: dict[str, str] = {
    "critical": "Critical",
    "high": "High",
    "medium": "Medium",
    "low": "Low",
    "info": "Informational",
}


def _one_liner_location(finding: FindingCtx) -> str:
    """The finding's location for the one-liner: ``host:port`` when both are present, else the host alone,
    else the URL (web scope), else "the tested scope" as the floor so the sentence never dangles. Pure
    read of ``FindingCtx`` fields, no lookups."""
    host_s = (getattr(finding, "target_host", None) or "").strip()
    port_s = str(getattr(finding, "target_port", None) or "").strip()
    if host_s and port_s:
        return f"{host_s}:{port_s}"
    if host_s:
        return host_s
    url_s = (getattr(finding, "target_url", None) or "").strip()
    return url_s or "the tested scope"


def _cve_clause(cve_ids: list[str]) -> str:
    """", CVE-..." (one) / ", CVE-A and CVE-B" (several, oxford-joined) / "" (none). Each id already
    says "CVE", so a "CVEs" label would just stutter (voice: cut the word that adds no meaning). No CVE
    is silence, not a safety claim."""
    ids = [str(c).strip() for c in (cve_ids or []) if str(c).strip()]
    if not ids:
        return ""
    return f", {_oxford(ids)}"


def _kev_sentence(finding: FindingCtx) -> str:
    """The KEV sentence (leading space), only when the finding's threat-intel marks a CISA-KEV CVE. The
    ``as_of`` date stays in the report's threat-intel chip, not this sentence (Ghostwriter). The
    exploiteer verdict tier (poc/weaponized/active) will add its own sentence here once LOT-48/1 enriches
    the finding with it; today KEV is the only exploitability signal ``FindingCtx`` carries."""
    ti = getattr(finding, "threat_intel", None)
    if isinstance(ti, dict) and ti.get("kev"):
        return " Listed in CISA KEV as known exploited."
    return ""


def render_finding_one_liner(finding: FindingCtx) -> str:
    """One deterministic sentence summarizing a single finding, over its ``FindingCtx`` fields (severity,
    location, CVE, KEV). No model; same finding -> same sentence. Ghostwriter's shape (LOT-72)."""
    severity = (getattr(finding, "severity", "") or "").strip().lower()
    rendered = _FINDING_ONE_LINER_TMPL.render(
        severity_word=_SEVERITY_WORDS.get(severity, severity.capitalize() if severity else "Unrated"),
        location=_one_liner_location(finding),
        cve_clause=_cve_clause(list(getattr(finding, "cve_ids", None) or [])),
        kev_sentence=_kev_sentence(finding),
    )
    return _collapse_ws(rendered)


def ai_polish_enabled() -> bool:
    """Master switch for the optional AI prose-polish seam. OFF unless BOTH the env flag is explicitly
    truthy AND the host exposes an AI hook. Default -> OFF, so the report is 100% deterministic and the
    seam is dormant. Reading the env every call (not at import) keeps a test's monkeypatch honest."""
    flag = os.environ.get(_AI_POLISH_ENV, "").strip().lower()
    if flag not in ("1", "true", "yes", "on"):
        return False
    return host.host_hook(_AI_HOOK) is not None


def polish(text: str, *, kind: str, enable: bool | None = None, ai_hook=None) -> str:
    """OPTIONAL prose-flow polish over ALREADY-RENDERED text. Identity by default.

    Contract (LOT-48 vision §2, plan §4):
    - It is OFF by default. With ``enable`` unset it consults ``ai_polish_enabled()``, which is OFF
      unless an operator both sets the env flag AND a provider hook is present.
    - It is NEVER in the decision path. It may only rephrase ``text`` for flow; it cannot add, drop,
      reorder, reclassify, or re-severity any fact. The deterministic ``text`` is the source of truth.
    - It FAILS CLOSED to the input: no provider, an empty result, or ANY exception -> ``text`` unchanged.
      So with the provider disabled the report is byte-for-byte identical.
    - It is deletable: nothing else in the engine depends on it, and the report is complete without it.

    ``kind`` labels the surface (e.g. "exec_summary") for a provider prompt; it never alters the fact
    content. ``ai_hook`` is injectable for tests; production resolves the host hook lazily.
    """
    if enable is None:
        enable = ai_polish_enabled()
    if not enable:
        return text
    hook = ai_hook if ai_hook is not None else host.host_hook(_AI_HOOK)
    if hook is None:
        return text
    try:
        # One batched call over the assembled text. The hook may return a string or an iterable of
        # chunks (the ai_stream shape); accept either. Anything else, or an empty result, keeps ``text``.
        out = hook(text=text, kind=kind)
        if out is None:
            return text
        polished = out if isinstance(out, str) else "".join(str(chunk) for chunk in out)
        polished = polished.strip()
        return polished or text
    except Exception:
        # A polish failure must never break a deterministic report that is already complete and valid.
        return text
