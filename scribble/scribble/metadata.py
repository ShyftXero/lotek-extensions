"""Structured finding metadata — references + CVE/CWE/OWASP + threat-intel (map #616, #624/#625).

Pure, offline, no-network helpers that turn the loose values #617 preserved only in the verbatim
``source_facts`` snapshot into the TYPED shapes ``EngagementFinding`` now stores and the renderers show:

  * **references** (#624) — a list of ``{label, url, source, suppressed}`` VALUE OBJECTS (not a child
    entity). Promote unions the matched library template's refs + the scan ``DTO.references``, deduped by
    NORMALIZED url, source-tagged (``template``/``scan``/``author``). An operator wins on collision.
  * **cve_ids / cwe_ids** (#625) — normalized, deduped id lists. ``cve_ids`` from ``DTO.cve`` (a scalar,
    superset to a list); ``cwe_ids`` from ``DTO.facts["cwe"]`` (no DTO widening — ``facts`` is part of the
    frozen #617 boundary).
  * **owasp_categories** (#625) — DERIVED from ``cwe_ids`` via a static offline CWE→OWASP-Top-10-2021 map
    (no network, no LLM), plus author override.
  * **threat_intel** (#625) — a DATED snapshot ``{as_of, source, cves:{CVE:{kev, epss, …}}}``, NOT bare
    ``kev``/``epss`` columns: KEV/EPSS change over time and their source (the exploiteer extension) is
    OPTIONAL, so a stored ``kev=true`` with no ``as_of`` is a lie the moment the catalog moves (#495 /
    the standing-prose honesty rule). ``build_threat_intel`` is pure — it takes an already-fetched feed
    and returns the snapshot (or ``None``); the LIVE exploiteer fetch is a caller's job and degrades to
    ``None`` when exploiteer is absent.

Everything here is deterministic and offline so a test grades it against hand-written INPUTS (the edd
"simulate inputs, never outputs" rule) — never against a faked scanner or exploiteer response.
"""

from __future__ import annotations

import re
from typing import Any

# Reference ``source`` axis (#624/#617 origin): where a reference came from.
REF_SOURCE_TEMPLATE = "template"   # the matched library VulnerabilityTemplate.references
REF_SOURCE_SCAN = "scan"           # the scan finding's DTO.references
REF_SOURCE_AUTHOR = "author"       # added/edited by a human in the finding editor
_REF_SOURCES = frozenset({REF_SOURCE_TEMPLATE, REF_SOURCE_SCAN, REF_SOURCE_AUTHOR})

# Bound on a finding's stored references. The authoring surfaces cap the INPUT before they call in (api_pat
# `_REFERENCE_LIST_MAX`), but ``merge_references`` also runs at PROMOTE time on ``DTO.references`` — scan
# tool output, which is attacker-influenceable and unbounded (host_contract). A degenerate scan finding
# citing 200k references must not build a 200k-object column, so the merge stops here too. Matches
# api_pat's cap so the two paths agree.
MAX_REFERENCES = 500

# Same DoS bound for the CVE/CWE id lists. ``cve_ids``/``cwe_ids`` are seeded at PROMOTE time from
# ``DTO.cve`` / ``DTO.facts["cwe"]`` -- attacker-influenceable, unbounded scan output -- so the
# normalizers cap their output here, exactly as ``merge_references`` caps references. (The authoring
# path also rejects an oversized list up front in api_pat, but promote has no such gate.)
MAX_IDS = MAX_REFERENCES

_CVE_RE = re.compile(r"CVE-\d{4}-\d{4,}", re.IGNORECASE)
_CWE_RE = re.compile(r"CWE-\d+", re.IGNORECASE)
_URLISH_RE = re.compile(r"^[a-z][a-z0-9+.-]*://", re.IGNORECASE)


# ── CVE / CWE normalization ──────────────────────────────────────────────────────────────────────────

def normalize_cve_ids(values: Any) -> list[str]:
    """A normalized, order-preserving, deduped list of ``CVE-YYYY-NNNN…`` ids from ``values`` (a scalar,
    a list, or ``None``). Anything that does not CONTAIN a CVE token is dropped rather than guessed —
    ``DTO.cve`` is usually a bare id but may arrive as free text, so extract rather than trust."""
    out: list[str] = []
    seen: set[str] = set()
    for raw in _as_str_list(values):
        for m in _CVE_RE.findall(raw):
            cve = m.upper()
            if cve not in seen:
                seen.add(cve)
                out.append(cve)
                if len(out) >= MAX_IDS:  # bound vs unbounded scan output (DTO.cve)
                    return out
    return out


def normalize_cwe_ids(values: Any) -> list[str]:
    """A normalized, order-preserving, deduped list of ``CWE-NNN`` ids. ``DTO.facts["cwe"]`` (nuclei's
    classification) may be a scalar, a list, or ``"CWE-79"``/``"79"``-shaped — extract every CWE token,
    and also accept a bare integer-ish value by prefixing ``CWE-``."""
    out: list[str] = []
    seen: set[str] = set()
    for raw in _as_str_list(values):
        found = _CWE_RE.findall(raw)
        if not found and raw.strip().isdigit():
            found = [f"CWE-{raw.strip()}"]
        for m in found:
            cwe = m.upper().replace(" ", "")
            if cwe not in seen:
                seen.add(cwe)
                out.append(cwe)
                if len(out) >= MAX_IDS:  # bound vs unbounded scan output (DTO.facts["cwe"])
                    return out
    return out


def cwe_url(cwe_id: str) -> str:
    """The canonical MITRE definition URL for ``CWE-79`` → ``…/definitions/79.html``; ``""`` if it isn't a
    CWE id. Built inline rather than importing lotek core's ``normalizer._cwe_reference`` — scribble never
    imports lotek (the boundary is duck-typed), and the URL shape is a one-liner."""
    m = re.fullmatch(r"CWE-(\d+)", (cwe_id or "").strip().upper())
    return f"https://cwe.mitre.org/data/definitions/{m.group(1)}.html" if m else ""


def cve_url(cve_id: str) -> str:
    """NVD detail URL for a CVE id; ``""`` if it isn't one."""
    cid = (cve_id or "").strip().upper()
    return f"https://nvd.nist.gov/vuln/detail/{cid}" if _CVE_RE.fullmatch(cid) else ""


# ── CWE → OWASP Top 10 (2021) ──────────────────────────────────────────────────────────────────────

# OWASP's published per-category CWE lists (2021 edition). Not every CWE OWASP lists is here — this is the
# subset a lotek scanner (nuclei/dalfox/nikto/…) realistically classifies a finding as, which is what the
# ``owasp_categories`` derivation needs. A CWE not in the map contributes no category (omit-when-empty),
# never a wrong one.
#
# ponytail: a static dict, not a downloaded/generated table. If a scanner starts emitting a CWE not here,
# ADD the row (source: https://owasp.org/Top10/ per-category "List of Mapped CWEs") — do not reach for a
# network lookup or an LLM. Keyed by the bare CWE number (int) to keep the literal terse.
OWASP_2021 = {
    "A01:2021": "Broken Access Control",
    "A02:2021": "Cryptographic Failures",
    "A03:2021": "Injection",
    "A04:2021": "Insecure Design",
    "A05:2021": "Security Misconfiguration",
    "A06:2021": "Vulnerable and Outdated Components",
    "A07:2021": "Identification and Authentication Failures",
    "A08:2021": "Software and Data Integrity Failures",
    "A09:2021": "Security Logging and Monitoring Failures",
    "A10:2021": "Server-Side Request Forgery (SSRF)",
}

_CWE_TO_OWASP: dict[int, str] = {
    # A01 Broken Access Control
    22: "A01:2021", 23: "A01:2021", 35: "A01:2021", 200: "A01:2021", 201: "A01:2021",
    284: "A01:2021", 285: "A01:2021", 352: "A01:2021", 359: "A01:2021", 425: "A01:2021",
    441: "A01:2021", 497: "A01:2021", 538: "A01:2021", 552: "A01:2021", 566: "A01:2021",
    601: "A01:2021", 639: "A01:2021", 668: "A01:2021", 862: "A01:2021", 863: "A01:2021",
    913: "A01:2021", 922: "A01:2021",
    # A02 Cryptographic Failures
    261: "A02:2021", 296: "A02:2021", 310: "A02:2021", 319: "A02:2021", 321: "A02:2021",
    326: "A02:2021", 327: "A02:2021", 328: "A02:2021", 329: "A02:2021", 330: "A02:2021",
    331: "A02:2021", 335: "A02:2021", 916: "A02:2021", 759: "A02:2021", 760: "A02:2021",
    # A03 Injection
    20: "A03:2021", 74: "A03:2021", 75: "A03:2021", 77: "A03:2021", 78: "A03:2021",
    79: "A03:2021", 80: "A03:2021", 83: "A03:2021", 87: "A03:2021", 88: "A03:2021",
    89: "A03:2021", 90: "A03:2021", 91: "A03:2021", 93: "A03:2021", 94: "A03:2021",
    95: "A03:2021", 96: "A03:2021", 97: "A03:2021", 98: "A03:2021", 113: "A03:2021",
    116: "A03:2021", 138: "A03:2021", 564: "A03:2021", 643: "A03:2021", 917: "A03:2021",
    # A04 Insecure Design
    209: "A04:2021", 256: "A04:2021", 501: "A04:2021", 522: "A04:2021", 602: "A04:2021",
    840: "A04:2021", 1021: "A04:2021",
    # A05 Security Misconfiguration
    2: "A05:2021", 11: "A05:2021", 13: "A05:2021", 15: "A05:2021", 16: "A05:2021",
    260: "A05:2021", 315: "A05:2021", 520: "A05:2021", 526: "A05:2021", 537: "A05:2021",
    541: "A05:2021", 611: "A05:2021", 614: "A05:2021", 756: "A05:2021", 776: "A05:2021",
    942: "A05:2021", 1004: "A05:2021", 1032: "A05:2021", 1174: "A05:2021",
    # A06 Vulnerable and Outdated Components
    937: "A06:2021", 1035: "A06:2021", 1104: "A06:2021",
    # A07 Identification and Authentication Failures
    255: "A07:2021", 259: "A07:2021", 287: "A07:2021", 288: "A07:2021", 290: "A07:2021",
    294: "A07:2021", 295: "A07:2021", 297: "A07:2021", 300: "A07:2021", 302: "A07:2021",
    304: "A07:2021", 306: "A07:2021", 307: "A07:2021", 346: "A07:2021", 384: "A07:2021",
    521: "A07:2021", 613: "A07:2021", 620: "A07:2021", 640: "A07:2021", 798: "A07:2021",
    # A08 Software and Data Integrity Failures
    345: "A08:2021", 353: "A08:2021", 426: "A08:2021", 494: "A08:2021", 502: "A08:2021",
    565: "A08:2021", 784: "A08:2021", 829: "A08:2021", 830: "A08:2021", 915: "A08:2021",
    # A09 Security Logging and Monitoring Failures
    117: "A09:2021", 223: "A09:2021", 532: "A09:2021", 778: "A09:2021",
    # A10 Server-Side Request Forgery
    918: "A10:2021",
}


def derive_owasp(cwe_ids: Any) -> list[str]:
    """The OWASP-Top-10-2021 category ids (``"A03:2021"``, …) a finding's CWEs map to, order-preserving
    and deduped. A CWE with no mapping contributes nothing (omit-when-empty). Deterministic + offline."""
    out: list[str] = []
    seen: set[str] = set()
    for cwe in normalize_cwe_ids(cwe_ids):
        num = int(cwe.split("-", 1)[1])
        cat = _CWE_TO_OWASP.get(num)
        if cat and cat not in seen:
            seen.add(cat)
            out.append(cat)
    return out


def owasp_label(category_id: str) -> str:
    """``"A03:2021 – Injection"`` for a known category id; the bare id if unknown; ``""`` if empty."""
    cid = (category_id or "").strip()
    name = OWASP_2021.get(cid)
    return f"{cid} – {name}" if name else cid


# ── references (value objects) ─────────────────────────────────────────────────────────────────────

def coerce_reference(value: Any, *, default_source: str = REF_SOURCE_AUTHOR) -> dict | None:
    """Coerce ONE reference into a ``{label, url, source, suppressed}`` value object, or ``None`` if it
    carries no usable content. Accepts:

      * a plain string — a URL (``label`` = the url, per the decision's "a bare-URL reference uses the URL
        as its own label") or a non-URL label like ``"CWE-79"`` (stored with an empty ``url`` → renders as
        plain text, not a link).
      * a dict — an already-shaped value object; unknown/blank ``source`` falls back to ``default_source``,
        ``suppressed`` is coerced to bool.

    Bounded so a hostile body can't blow the column: ``label``/``url`` are ``str()``-coerced and clipped.
    """
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        is_url = bool(_URLISH_RE.match(text))
        return {"label": text[:_LABEL_MAX], "url": (text[:_URL_MAX] if is_url else ""),
                "source": default_source, "suppressed": False}
    if isinstance(value, dict):
        url = str(value.get("url") or "").strip()[:_URL_MAX]
        label = str(value.get("label") or "").strip()[:_LABEL_MAX] or url
        if not label and not url:
            return None
        source = value.get("source")
        source = source if source in _REF_SOURCES else default_source
        return {"label": label, "url": url, "source": source,
                "suppressed": bool(value.get("suppressed"))}
    return None


_LABEL_MAX = 512
_URL_MAX = 2048


def _ref_key(ref: dict) -> str:
    """The dedup key for a reference: its NORMALIZED url when it has one (scheme+host lowercased, trailing
    slash and fragment stripped), else its normalized label. Two refs with the same key are "the same
    reference" for the union below."""
    url = ref.get("url") or ""
    if url:
        u = url.strip().rstrip("/")
        u = u.split("#", 1)[0]
        # lowercase only the scheme+authority; a path can be case-sensitive.
        m = re.match(r"^([a-z][a-z0-9+.-]*://[^/]+)(.*)$", u, re.IGNORECASE)
        return (m.group(1).lower() + m.group(2)) if m else u.lower()
    return "label:" + (ref.get("label") or "").strip().lower()


def merge_references(*groups: Any, sources: tuple[str, ...] | None = None) -> list[dict]:
    """Union several reference lists into deduped value objects, source-tagging each group and keeping the
    FIRST occurrence of a given normalized key (so earlier groups win a collision — pass the
    higher-precedence group first). ``sources`` names the origin for each positional group; a group whose
    elements are already value objects with their own ``source`` keeps it.

    Used by promote as ``merge_references(template_refs, scan_refs, sources=("template", "scan"))`` — a
    template ref and a scan ref citing the same URL collapse to one, tagged ``template`` (first). An
    OPERATOR ref (source ``author``) is never merged here: re-promote is fill-NULL-only and never re-runs
    this on an edited row, so an operator edit/suppress is never clobbered (#617 Q5, #624 "operator wins").
    """
    out: list[dict] = []
    seen: set[str] = set()
    for i, group in enumerate(groups):
        src = sources[i] if sources and i < len(sources) else REF_SOURCE_AUTHOR
        for raw in group or []:
            ref = coerce_reference(raw, default_source=src)
            if ref is None:
                continue
            key = _ref_key(ref)
            if key in seen:
                continue
            seen.add(key)
            out.append(ref)
            if len(out) >= MAX_REFERENCES:  # bound the column vs an unbounded scan references list
                return out
    return out


def visible_references(references: Any) -> list[dict]:
    """The non-suppressed references, in order — what the renderers show. ``source`` is carried but the
    renderers hide it by default (#624 Q4). An empty result means the References block is OMITTED."""
    return [r for r in (references or []) if isinstance(r, dict) and not r.get("suppressed")]


# ── threat intelligence (dated snapshot) ────────────────────────────────────────────────────────────

def build_threat_intel(cve_ids: Any, feed: Any, *, as_of: str, source: str) -> dict | None:
    """A dated KEV/EPSS snapshot for a finding's CVEs, or ``None`` when there is nothing to record.

    PURE: ``feed`` is an already-fetched ``{CVE: {kev, kev_date_added, epss, epss_percentile}}`` mapping
    (the exploiteer verdict feed keyed by CVE). This function does NOT fetch it — the live exploiteer read
    is the caller's job and degrades to ``feed=None`` → ``None`` here when exploiteer is absent, so an
    unenriched finding gets no snapshot and renders byte-identically to today. ``as_of`` is MANDATORY so
    the report says "KEV as of <date>" and never asserts a stale fact as current (#495 / standing prose).

    Only CVEs the feed actually has an entry for are recorded; a finding whose CVEs are all absent from the
    feed yields ``None`` (no snapshot, not an empty one).
    """
    if not feed:
        return None
    cves: dict[str, dict] = {}
    for cve in normalize_cve_ids(cve_ids):
        entry = feed.get(cve) if isinstance(feed, dict) else None
        if not isinstance(entry, dict):
            continue
        rec: dict[str, Any] = {"kev": bool(entry.get("kev"))}
        if entry.get("kev_date_added"):
            rec["kev_date_added"] = str(entry["kev_date_added"])
        epss = entry.get("epss")
        if isinstance(epss, (int, float)) and not isinstance(epss, bool):
            rec["epss"] = float(epss)
        pct = entry.get("epss_percentile")
        if isinstance(pct, (int, float)) and not isinstance(pct, bool):
            rec["epss_percentile"] = float(pct)
        cves[cve] = rec
    if not cves:
        return None
    return {"as_of": as_of, "source": source, "cves": cves}


def threat_intel_display(threat_intel: Any) -> dict | None:
    """Reduce a ``threat_intel`` snapshot to what a renderer needs: ``{kev, epss, as_of, source}`` where
    ``kev`` is true if ANY of the finding's CVEs is KEV-listed and ``epss`` is the MAX EPSS across them
    (the finding's worst case). ``None`` when there is nothing to show — the chips are omitted."""
    if not isinstance(threat_intel, dict):
        return None
    cves = threat_intel.get("cves")
    if not isinstance(cves, dict) or not cves:
        return None
    kev = any(isinstance(c, dict) and c.get("kev") for c in cves.values())
    # a bool is not an EPSS score -- mirror build_threat_intel's exclusion.
    epss_values = [c["epss"] for c in cves.values()
                   if isinstance(c, dict) and isinstance(c.get("epss"), (int, float))
                   and not isinstance(c.get("epss"), bool)]
    epss = max(epss_values) if epss_values else None
    if not kev and epss is None:
        return None
    return {"kev": kev, "epss": epss, "as_of": threat_intel.get("as_of"),
            "source": threat_intel.get("source")}


def _as_str_list(values: Any) -> list[str]:
    """``values`` (scalar / list / None / other) as a list of non-empty strings — the tolerant front door
    every normalizer above shares."""
    if values is None:
        return []
    if isinstance(values, (list, tuple)):
        items = values
    else:
        items = [values]
    return [str(v) for v in items if v is not None and str(v).strip()]
