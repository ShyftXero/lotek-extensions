"""Unit tests for ``scribble.metadata`` — the pure, offline helpers behind #624 references and #625
CVE/CWE/OWASP/threat-intel columns (map #616).

Everything here is graded against hand-written INPUTS (the edd "simulate inputs, never outputs" rule):
a CVE string, a CWE list, a synthetic KEV/EPSS feed. Nothing fakes a scanner or an exploiteer response.
"""

from __future__ import annotations

from scribble import metadata as m

# ── CVE / CWE normalization ──────────────────────────────────────────────────────────────────────────

def test_normalize_cve_ids_upper_dedup_extract():
    assert m.normalize_cve_ids("cve-2021-44228") == ["CVE-2021-44228"]
    # a scalar, a list, embedded-in-text, and duplicates all normalize + dedupe order-preserving.
    assert m.normalize_cve_ids(
        ["CVE-2020-0001", "see CVE-2020-0001 and cve-2019-1234", None, "not-a-cve"]
    ) == ["CVE-2020-0001", "CVE-2019-1234"]
    assert m.normalize_cve_ids(None) == []


def test_normalize_cwe_ids_accepts_scalar_list_and_bare_int():
    assert m.normalize_cwe_ids("CWE-79") == ["CWE-79"]
    assert m.normalize_cwe_ids("89") == ["CWE-89"]              # nuclei sometimes emits a bare number
    assert m.normalize_cwe_ids(["cwe-79", "CWE-79", "352"]) == ["CWE-79", "CWE-352"]
    assert m.normalize_cwe_ids(None) == []


def test_cwe_and_cve_urls():
    assert m.cwe_url("CWE-79") == "https://cwe.mitre.org/data/definitions/79.html"
    assert m.cwe_url("nonsense") == ""
    assert m.cve_url("CVE-2021-44228") == "https://nvd.nist.gov/vuln/detail/CVE-2021-44228"
    assert m.cve_url("CWE-79") == ""


# ── CWE -> OWASP Top 10 (2021) ─────────────────────────────────────────────────────────────────────

def test_derive_owasp_maps_known_cwes_and_dedupes():
    # CWE-79 (XSS) and CWE-89 (SQLi) both map to A03 Injection -> one category, deduped.
    assert m.derive_owasp(["CWE-79", "CWE-89"]) == ["A03:2021"]
    # CWE-22 path traversal -> A01 Broken Access Control; order follows the cwe list.
    assert m.derive_owasp(["CWE-89", "CWE-22"]) == ["A03:2021", "A01:2021"]


def test_derive_owasp_drops_unmapped_cwe():
    # a CWE with no OWASP mapping contributes NOTHING (omit-when-empty), never a wrong category.
    assert m.derive_owasp(["CWE-99999"]) == []
    assert m.derive_owasp([]) == []


def test_owasp_label():
    assert m.owasp_label("A03:2021") == "A03:2021 – Injection"
    assert m.owasp_label("A99:2021") == "A99:2021"   # unknown id -> bare id, never a made-up name


# ── references (value objects) ─────────────────────────────────────────────────────────────────────

def test_coerce_reference_string_url_vs_label():
    url = m.coerce_reference("https://owasp.org/xss")
    assert url == {"label": "https://owasp.org/xss", "url": "https://owasp.org/xss",
                   "source": "author", "suppressed": False}
    # a non-URL string (a bare label) keeps an empty url -> renders as plain text, not a link.
    label = m.coerce_reference("CWE-79", default_source="scan")
    assert label == {"label": "CWE-79", "url": "", "source": "scan", "suppressed": False}
    assert m.coerce_reference("   ") is None


def test_coerce_reference_dict_keeps_suppress_and_validates_source():
    ref = m.coerce_reference({"label": "KB123", "url": "https://v/kb", "source": "bogus",
                              "suppressed": True})
    assert ref == {"label": "KB123", "url": "https://v/kb", "source": "author", "suppressed": True}


def test_merge_references_dedupes_by_normalized_url_first_wins():
    merged = m.merge_references(
        ["https://ex/a", "https://ex/b/"],                  # template group
        ["https://ex/b", "https://ex/c", "CWE-79"],         # scan group
        sources=(m.REF_SOURCE_TEMPLATE, m.REF_SOURCE_SCAN),
    )
    urls = [(r["url"], r["source"]) for r in merged]
    # https://ex/b and https://ex/b/ are the SAME reference (trailing slash normalized in the dedup KEY):
    # they collapse to ONE, tagged template (first group wins), and the FIRST-SEEN original url is kept
    # verbatim (https://ex/b/) rather than a mangled/normalized form.
    assert ("https://ex/b/", "template") in urls
    assert ("https://ex/b", "scan") not in urls
    assert sum(1 for u, _ in urls if u in ("https://ex/b", "https://ex/b/")) == 1
    assert ("https://ex/a", "template") in urls and ("https://ex/c", "scan") in urls
    assert merged[-1] == {"label": "CWE-79", "url": "", "source": "scan", "suppressed": False}


def test_visible_references_filters_suppressed():
    refs = [
        {"label": "a", "url": "https://a", "source": "scan", "suppressed": False},
        {"label": "b", "url": "https://b", "source": "scan", "suppressed": True},
    ]
    assert [r["label"] for r in m.visible_references(refs)] == ["a"]
    assert m.visible_references([]) == []


def test_merge_references_is_bounded():
    # a degenerate scan references list (attacker-influenceable, unbounded) must not build an unbounded
    # column — the merge stops at MAX_REFERENCES.
    huge = [f"https://ex/{i}" for i in range(m.MAX_REFERENCES + 50)]
    merged = m.merge_references(huge, sources=(m.REF_SOURCE_SCAN,))
    assert len(merged) == m.MAX_REFERENCES


# ── threat intelligence (dated snapshot) ─────────────────────────────────────────────────────────────

def _feed() -> dict:
    """A SYNTHETIC exploiteer-shaped verdict feed keyed by CVE — a hand-written INPUT, never a captured
    exploiteer response."""
    return {
        "CVE-2021-44228": {"kev": True, "kev_date_added": "2021-12-10", "epss": 0.975,
                           "epss_percentile": 0.999},
        "CVE-2020-0001": {"kev": False, "epss": 0.02},
    }


def test_build_threat_intel_records_only_feed_hits_with_as_of():
    snap = m.build_threat_intel(
        ["CVE-2021-44228", "CVE-2020-0001", "CVE-1999-9999"], _feed(),
        as_of="2026-09-04", source="exploiteer",
    )
    assert snap["as_of"] == "2026-09-04" and snap["source"] == "exploiteer"
    # CVE-1999-9999 is not in the feed -> not recorded (no invented entry).
    assert set(snap["cves"]) == {"CVE-2021-44228", "CVE-2020-0001"}
    assert snap["cves"]["CVE-2021-44228"]["kev"] is True
    assert snap["cves"]["CVE-2021-44228"]["kev_date_added"] == "2021-12-10"


def test_build_threat_intel_degrades_to_none_without_a_feed():
    # exploiteer absent -> feed is None -> NO snapshot (an unenriched finding renders byte-identically).
    assert m.build_threat_intel(["CVE-2021-44228"], None, as_of="x", source="y") is None
    # a finding whose CVEs are all absent from the feed -> None, not an empty snapshot.
    assert m.build_threat_intel(["CVE-1999-9999"], _feed(), as_of="x", source="y") is None


def test_threat_intel_display_reduces_to_worst_case():
    snap = m.build_threat_intel(["CVE-2021-44228", "CVE-2020-0001"], _feed(),
                                as_of="2026-09-04", source="exploiteer")
    disp = m.threat_intel_display(snap)
    assert disp["kev"] is True                    # any CVE KEV-listed
    assert disp["epss"] == 0.975                  # MAX epss across the finding's CVEs
    assert disp["as_of"] == "2026-09-04"
    assert m.threat_intel_display(None) is None
