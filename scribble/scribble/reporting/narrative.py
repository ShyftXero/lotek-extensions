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

The strings below are Hardcopy's placeholder voice; the final voice pass against the Lotek voice
guide is Ghostwriter's (LOT-48/6).
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


# The executive summary. Order is fixed: tested -> found -> could-not-run (plan §5). Whitespace is
# collapsed after rendering, so the template is laid out for reading, not for output spacing.
_EXEC_SUMMARY_SRC = """
{%- if total == 0 -%}
  {%- if coverage_limited_jobs > 0 -%}
    This assessment of {{ company }} recorded no findings in the portions of scope that were assessed.
    {{ coverage_limited_jobs }} scan {{ "job" if coverage_limited_jobs == 1 else "jobs" }} could not be
    assessed and {{ "was" if coverage_limited_jobs == 1 else "were" }} carried on an acknowledged
    coverage gap, recorded under the scan-coverage limitations below. The absence of findings there is
    not evidence those systems are free of the issues the unassessed modules would have detected.
  {%- else -%}
    This assessment of {{ company }} did not identify any findings within the tested scope.
  {%- endif -%}
{%- else -%}
  This assessment of {{ company }} identified {{ total }} {{ finding_word }}
  across the environment{{ severity_clause }}.
  {%- if path_count > 0 %} The engagement traced {{ path_count }} cradle-to-Domain-Admin
  attack {{ "path" if path_count == 1 else "paths" }} through the discovered findings.{% endif %}
  {%- if top_titles %} The most significant exposures were {{ top_titles | join("; ") }}.
  {%- else %} No critical or high-risk issues were identified; findings were limited to
  lower-severity observations.{% endif %}
  {%- if coverage_limited_jobs > 0 %} Coverage was incomplete: {{ coverage_limited_jobs }}
  scan {{ "job" if coverage_limited_jobs == 1 else "jobs" }} could not be assessed and
  {{ "is" if coverage_limited_jobs == 1 else "are" }} recorded under the scan-coverage
  limitations below, so these findings are not a complete picture of those systems.{% endif %}
{%- endif -%}
"""

# The per-finding one-liner. Every clause is omit-when-empty, so a finding with no host or CVE still
# reads cleanly. ``exploit_tier`` is the exploitability qualifier derived in ``render_finding_one_liner``.
_FINDING_ONE_LINER_SRC = """
{{ severity_label }}-severity finding
{%- if exploit_tier %} ({{ exploit_tier }}){% endif %}
{%- if host %} on {{ host }}{% endif %}
{%- if cve %} [{{ cve }}]{% endif %}: {{ title }}.
"""

_EXEC_SUMMARY_TMPL = _ENV.from_string(_EXEC_SUMMARY_SRC)
_FINDING_ONE_LINER_TMPL = _ENV.from_string(_FINDING_ONE_LINER_SRC)


def _collapse_ws(text: str) -> str:
    """Collapse every run of whitespace to a single space and strip the ends -- so the template can be
    written for legibility and still emit one clean paragraph/sentence."""
    return " ".join(text.split())


def _severity_clause(rollup: SeverityRollup) -> str:
    """The ", including N critical and M high-risk issue(s)" tail, or "" when there are none. Reads the
    LIVE-only counts already computed in ``rollup`` (lotek#618), so it never overstates present risk."""
    crit = rollup.counts.get("critical", 0)
    high = rollup.counts.get("high", 0)
    bits: list[str] = []
    if crit:
        bits.append(f"{crit} critical")
    if high:
        bits.append(f"{high} high-risk")
    if not bits:
        return ""
    issue_word = "issue" if (crit + high) == 1 else "issues"
    return f", including {' and '.join(bits)} {issue_word}"


def render_executive_summary(
    *,
    company_name: str,
    rollup: SeverityRollup,
    top_titles: list[str],
    path_count: int = 0,
    coverage_limited_jobs: int = 0,
) -> str:
    """The executive-summary paragraph, rendered deterministically from structured facts (no model).

    ``company_name``           the client/company name, or "" (falls back to a neutral phrase).
    ``rollup``                  the ``SeverityRollup`` (LIVE-only severity counts + total).
    ``top_titles``             worst LIVE finding titles, already ordered and capped by the caller.
    ``path_count``             discovered cradle-to-Domain-Admin attack paths (``len(ctx.chains)``).
    ``coverage_limited_jobs``  scan jobs promoted on an acknowledged coverage gap (plan §5). > 0 ->
                               the summary states the could-not-run posture and NEVER reads "no issues".
    """
    company = company_name or "the target environment"
    total = rollup.total
    rendered = _EXEC_SUMMARY_TMPL.render(
        company=company,
        total=total,
        finding_word=("finding" if total == 1 else "findings"),
        severity_clause=_severity_clause(rollup),
        top_titles=list(top_titles or []),
        path_count=max(0, int(path_count or 0)),
        coverage_limited_jobs=max(0, int(coverage_limited_jobs or 0)),
    )
    return _collapse_ws(rendered)


def _one_liner_host(finding: FindingCtx) -> str:
    """The tightest host locator the finding carries: ``host:port`` when both are present, else the URL,
    else the host alone, else "". Pure read of ``FindingCtx`` fields, no lookups."""
    host_s = (getattr(finding, "target_host", None) or "").strip()
    port_s = str(getattr(finding, "target_port", None) or "").strip()
    if host_s and port_s:
        return f"{host_s}:{port_s}"
    if host_s:
        return host_s
    return (getattr(finding, "target_url", None) or "").strip()


def _exploit_tier(finding: FindingCtx) -> str:
    """A deterministic exploitability qualifier from the finding's own fields. Today the only tier
    signal carried on ``FindingCtx`` is threat-intel KEV (a CVE on CISA's Known-Exploited list); the
    exploiteer verdict tier (none/poc/weaponized/active) plugs in here verbatim once LOT-48/1 enriches
    the finding with it. "" when there is nothing to assert -- never a guess."""
    ti = getattr(finding, "threat_intel", None)
    if isinstance(ti, dict) and ti.get("kev"):
        return "known-exploited"
    return ""


def render_finding_one_liner(finding: FindingCtx) -> str:
    """One deterministic sentence summarizing a single finding, over its ``FindingCtx`` fields
    (severity, exploitability tier, host, CVE, title). No model; same finding -> same sentence."""
    severity = (getattr(finding, "severity", "") or "").strip()
    cve_ids = list(getattr(finding, "cve_ids", None) or [])
    rendered = _FINDING_ONE_LINER_TMPL.render(
        severity_label=(severity.capitalize() if severity else "Unrated"),
        exploit_tier=_exploit_tier(finding),
        host=_one_liner_host(finding),
        cve=(str(cve_ids[0]) if cve_ids else ""),
        title=(getattr(finding, "title", "") or "").strip(),
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
