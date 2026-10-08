#!/usr/bin/env python3
"""Transform an exported finding-template + test-plan corpus into scribble seed files.

Deterministic + re-runnable: given the same export it emits byte-identical output, so a re-export
re-merges the same way and the diff is reviewable.

Outputs (into scribble/scribble/seed/):
  * curated_vulnerabilities.json   -- merged vuln templates (consumed by seed/loader.import_curated_templates)
  * checklists/<slug>.json    -- one coverage checklist per active assessment bucket

Merge policy (approved): TRUE VARIANTS ONLY. A "family" is the set of templates whose name differs
only by a `` - <tech>`` / ``[<platform>]`` suffix. Distinct root causes keep distinct base names and
are therefore never merged (e.g. "Insecure SSL/TLS Configuration" vs "...Certificate Configuration").
Within a family: canonical body = the un-suffixed member if present, else the highest-severity then
longest body; variant-specific text is folded into a "Variants / affected technologies" list;
severity = max; references = union; applicability + CWE become tags.

Brace policy: scribble jinja-renders content, and any invalid ``{{ }}`` blanks the block. So:
  * ``PLACEHOLDER:CLIENT_NAME`` / ``{{client}}`` -> ``{{COMPANY_NAME}}`` (scribble builtin)
  * other ``PLACEHOLDER:FOO`` / unknown ``{{FOO}}`` -> single-brace ``{foo}`` (jinja-inert author fill-in)
  * ``{{ Consultant Note: ... }}`` -> stripped (authoring instruction, not report content)
  * single-brace ``{x}`` and ``{a | b}`` choices -> left as-is (jinja ignores single braces)
Known scribble tokens left as double-brace: COMPANY_NAME, AFFECTED, DOMAIN, TARGET_URL, TARGET_HOST,
MAX_SEVERITY, AFFECTED_COUNT, ACCOUNTS, OBJECTS.

Usage:
  python3 -I build_vuln_library.py --export <dir-with-deliverables_*+test-plans_*> --out <seed-dir>
"""
from __future__ import annotations

import argparse
import collections
import glob
import html as html_mod
import json
import os
import re

# ---------------------------------------------------------------------------
# token / brace normalization
# ---------------------------------------------------------------------------
KNOWN_TOKENS = {
    "COMPANY_NAME", "AFFECTED", "DOMAIN", "TARGET_URL", "TARGET_HOST",
    "MAX_SEVERITY", "AFFECTED_COUNT", "ACCOUNTS", "OBJECTS",
}
_CLIENT_RE = re.compile(r"PLACEHOLDER:\s*CLIENT[\\_ ]*NAME", re.IGNORECASE)
_PLACEHOLDER_RE = re.compile(r"PLACEHOLDER:\s*([A-Za-z0-9_\\]+)")
_DOUBLE_BRACE_RE = re.compile(r"\{\{\s*([^{}]*?)\s*\}\}")
# Consultant/authoring notes appear in many delimiter styles across the corpus:
# {{...}}, {...}, [...], <!--...-->, &lt;--...--&gt;, &lt;...  (optional **bold**). One stripper:
# opener (any kind) + optional markdown + "consultant note" + body up to the first matching close
# (or just before a </p>, zero-width so the tag survives).
_NOTE_RE = re.compile(
    r"(?:\{\{|\{|\[|<!?--|&(?:amp;)?lt;!?--?|&(?:amp;)?lt;|<)\s*(?:\*\*)?\s*consultant note"
    r".*?(?:\}\}|\}|\]|--&(?:amp;)?gt;|-->|&(?:amp;)?gt;|(?=</p>)|$)",
    re.IGNORECASE | re.DOTALL,
)


def _unwrap_placeholder(inner: str) -> str:
    pm = re.match(r"PLACEHOLDER:\s*(.+)", inner, re.IGNORECASE)
    return pm.group(1).replace("\\", "").strip() if pm else inner


def normalize_braces(text: str) -> str:
    if not text:
        return ""
    text = _NOTE_RE.sub("", text)

    def _db(m: re.Match) -> str:
        inner = _unwrap_placeholder(m.group(1).strip())
        key = inner.split("|")[0].split()[0].strip() if inner else ""
        if key.upper() in {"CLIENT_NAME", "CLIENT"} or key.lower() == "client":
            return "{{COMPANY_NAME}}"
        if key.upper() in KNOWN_TOKENS:
            return "{{" + key.upper() + "}}"
        return "{" + inner.lower() + "}"  # author fill-in, jinja-inert single brace

    text = _CLIENT_RE.sub("{{COMPANY_NAME}}", text)
    text = _PLACEHOLDER_RE.sub(lambda m: "{" + m.group(1).replace("\\", "").lower() + "}", text)
    # brace-collapse LAST, looped: fixes {{PLACEHOLDER:X}}, {PLACEHOLDER:X}->{{x}}, and {{{x}}} nesting,
    # mapping known tokens back to double-brace and leaving author fill-ins single-brace.
    for _ in range(5):
        new = _DOUBLE_BRACE_RE.sub(_db, text)
        if new == text:
            break
        text = new
    return text


# ---------------------------------------------------------------------------
# strip source-org attribution from any carried text (interim; the voice rewrite replaces prose wholesale)
# ---------------------------------------------------------------------------
_ORG_PHRASE_RE = re.compile(r"\bbishop[\s-]+fox('?s)?\b", re.IGNORECASE)
_ORG_TOKEN_RE = re.compile(r"bishop[\s-]*fox", re.IGNORECASE)  # concatenated artifacts: bishopfoxtest, @bishopfoxmail


def scrub_orgs(text: str) -> str:
    text = _ORG_PHRASE_RE.sub("the assessment team", text or "")
    return _ORG_TOKEN_RE.sub("example", text)


# ---------------------------------------------------------------------------
# light markdown-ish -> HTML (the export bodies mix <p>, "+ bullets", *em*, \r\n)
# ---------------------------------------------------------------------------
def md_to_html(text: str) -> str:
    text = scrub_orgs(normalize_braces(text or ""))
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if "<p" in text or "<ul" in text or "<li" in text or "<div" in text:
        return text.strip()  # already HTML-ish; trust it
    out: list[str] = []
    bullets: list[str] = []

    def flush() -> None:
        if bullets:
            out.append("<ul>" + "".join(f"<li>{html_mod.escape(b)}</li>" for b in bullets) + "</ul>")
            bullets.clear()

    for raw in text.split("\n"):
        line = raw.strip()
        if not line:
            flush()
            continue
        m = re.match(r"^[+*-]\s+(.*)$", line)
        if m:
            bullets.append(m.group(1).strip())
        else:
            flush()
            out.append(f"<p>{html_mod.escape(line)}</p>")
    flush()
    return "".join(out)


# ---------------------------------------------------------------------------
# severity
# ---------------------------------------------------------------------------
_SEV_RANK = {"info": 0, "informational_findings": 0, "low_findings": 1, "medium_findings": 2,
             "high_findings": 3, "critical_findings": 4}
_SEV_NAME = {0: "info", 1: "low", 2: "medium", 3: "high", 4: "critical"}


def sev_to_name(raw: str | None) -> str:
    return _SEV_NAME[_SEV_RANK.get((raw or "").strip(), 2)]


def sev_rank(raw: str | None) -> int:
    return _SEV_RANK.get((raw or "").strip(), 2)


# ---------------------------------------------------------------------------
# applicability: service_line -> bucket tags
# ---------------------------------------------------------------------------
# Applicability tags are `scope:`-prefixed (like `cwe:NNN`) so they never collide with a bare
# user/free-form tag name ("internal", "external") in the shared Tag table.
SL_TAGS = {
    "network-security": ["scope:external", "scope:internal"],
    "application-security": ["scope:webapp"],
    "cloud-security": ["scope:cloud"],
    "host-based-review": ["scope:internal"],
    "red-team": ["scope:red-team"],
    "architecture-security": ["scope:architecture"],
    "mobile-security": ["scope:mobile"],
    "generative-ai": ["scope:genai"],
    "wireless-security": ["scope:wireless"],
    "product-security": ["scope:product"],
}


def family_base(name: str) -> str:
    n = re.split(r"\s[—–-]\s", name)[0]
    n = re.sub(r"\s*[\[(].*?[\])]\s*$", "", n)
    return n.strip()


def variant_suffix(name: str, base: str) -> str:
    s = name[len(base):].strip() if name.startswith(base) else name
    return s.lstrip("—–-[( ").rstrip(")] ").strip() or name


# ---------------------------------------------------------------------------
# load export
# ---------------------------------------------------------------------------
def load_findings(export: str) -> list[dict]:
    rows = []
    for f in sorted(glob.glob(os.path.join(export, "deliverables_page*"))):
        for t in json.load(open(f)).get("templates", []):
            cc = t.get("current_content") or {}
            c = cc.get("content") or {}
            rows.append({
                "name": (cc.get("name") or "").strip(),
                "service_line": cc.get("service_line") or "",
                "category": cc.get("category"),
                "severity": cc.get("recommended_severity"),
                "cwe": c.get("cwe"),
                "cvss": c.get("cvss"),
                "details": ((c.get("details") or {}).get("body") or "") if isinstance(c.get("details"), dict) else (c.get("details") or ""),
                "definition": (c.get("definition") or {}).get("body", "") if isinstance(c.get("definition"), dict) else (c.get("definition") or ""),
                "recommendations": (c.get("recommendations") or {}).get("body", "") if isinstance(c.get("recommendations"), dict) else (c.get("recommendations") or ""),
                "resources": c.get("additional_resources") or [],
            })
    return [r for r in rows if r["name"]]


def build_vulns(rows: list[dict]) -> list[dict]:
    fam = collections.defaultdict(list)
    for r in rows:
        fam[family_base(r["name"])].append(r)

    out = []
    for base, members in sorted(fam.items()):
        # canonical: prefer exact-base member, else highest severity then longest details
        exact = [m for m in members if m["name"].strip() == base]
        canonical = (exact or sorted(members, key=lambda m: (sev_rank(m["severity"]), len(m["details"] or "")), reverse=True))[0]

        desc_html = md_to_html(canonical["details"])
        impact_html = md_to_html(canonical["definition"]) if canonical["definition"] else ""

        # fold true-variant specifics
        variants = [m for m in members if m is not canonical]
        if variants:
            items = []
            for v in sorted(variants, key=lambda m: m["name"]):
                suf = variant_suffix(v["name"], base)
                lead = re.sub(r"<[^>]+>", " ", md_to_html(v["details"]))
                lead = re.sub(r"\s+", " ", lead).strip()[:200]
                items.append(f"<li><strong>{html_mod.escape(suf)}:</strong> {html_mod.escape(lead)}</li>")
            desc_html += "<p><strong>Variants / affected technologies</strong></p><ul>" + "".join(items) + "</ul>"

        description = "# Description\n" + desc_html
        if impact_html:
            description += "\n# Impact\n" + impact_html
        description += "\n# Replication Steps\n"

        # tags: applicability (union of service_lines) + cwe + base category
        tags = set()
        for m in members:
            for tg in SL_TAGS.get(m["service_line"], []):
                tags.add(tg)
            if m["cwe"]:
                for cwe in re.findall(r"\d+", str(m["cwe"])):
                    tags.add(f"cwe:{cwe}")

        refs = []
        for m in members:
            for r0 in (m["resources"] or []):
                s = scrub_orgs(r0 if isinstance(r0, str) else (r0.get("url") or r0.get("label") or ""))
                if s and s not in refs:
                    refs.append(s)

        cvss = next((m["cvss"] for m in members if m.get("cvss")), None)
        cat = canonical["category"] or next((m["category"] for m in members if m["category"]), None)

        out.append({
            "name": base,
            "category": cat,
            "severity": sev_to_name(max(members, key=lambda m: sev_rank(m["severity"]))["severity"]),
            "cvss_score": (cvss if isinstance(cvss, (int, float)) else None),
            "cvss_vector": (cvss if isinstance(cvss, str) else None),
            "references": refs,
            "tags": sorted(tags),
            "variant_count": len(members),
            "Description": description,
            "Recommendation": md_to_html(canonical["recommendations"]),
        })
    return out


# ---------------------------------------------------------------------------
# checklists
# ---------------------------------------------------------------------------
# bucket -> (slug, name, category, assessment_type filter, tag/keyword predicate)
AUTH_RE = re.compile(r"authenticat|credential|logged.?in|ceded|post.?exploit|valid account|domain user|with access", re.IGNORECASE)
WEB_TAGS = {"Web Application Backend or API", "Web Application Frontend"}
THICK_TAG = "Locally-installed Software"


def load_testplans(export: str) -> list[dict]:
    rows = []
    for f in sorted(glob.glob(os.path.join(export, "test-plans_page*.json"))):
        for t in json.load(open(f)).get("templates", []):
            a = t.get("attributes") or {}
            rows.append({
                "title": t.get("title") or "",
                "desc": t.get("report_description") or "",
                "playbook": t.get("description") or "",
                "type": a.get("assessment_type"),
                "tags": a.get("tags") or [],
            })
    return [r for r in rows if r["title"]]


def item(tp: dict, section: str) -> dict:
    return {"section": section, "text": scrub_orgs(tp["title"]),
            "guidance": scrub_orgs((tp["desc"] or tp["playbook"] or "").strip()[:500])}


def build_checklists(tps: list[dict]) -> dict[str, dict]:
    def is_auth(tp): return bool(AUTH_RE.search(tp["desc"] + " " + tp["playbook"]))
    def is_web(tp): return bool(WEB_TAGS & set(tp["tags"]))
    def is_thick(tp): return THICK_TAG in tp["tags"]

    out = {}

    def mk(slug, name, category, desc, selected, sectioner):
        items = []
        for tp in selected:
            items.append(item(tp, sectioner(tp)))
        # stable order; assign order_index
        items.sort(key=lambda x: (x["section"] or "", x["text"]))
        for i, it in enumerate(items):
            it["order_index"] = i
        out[slug] = {"slug": slug, "name": name, "kind": "coverage", "category": category,
                     "description": desc, "items": items}

    ext = [t for t in tps if t["type"] == "external-penetration-test"]
    mk("external-network", "External Network Penetration Test", "external",
       "External/perimeter methodology.", ext,
       lambda t: (t["tags"][0] if t["tags"] else "General"))

    internal = [t for t in tps if t["type"] == "internal-penetration-test"]
    host = [t for t in tps if t["type"] == "host-based-review"]
    mk("internal-network-unauth", "Internal Network Penetration Test (Unauthenticated)", "internal",
       "Internal methodology, no credentials supplied.", [t for t in internal if not is_auth(t)],
       lambda t: (t["tags"][0] if t["tags"] else "Network"))
    mk("internal-ceded-access", "Internal Penetration Test (Ceded / Authenticated Access)", "internal",
       "Internal with supplied credentials + on-host review.", [t for t in internal if is_auth(t)] + host,
       lambda t: ("Host-based Review" if t["type"] == "host-based-review" else (t["tags"][0] if t["tags"] else "Authenticated")))

    app = [t for t in tps if t["type"] == "application-penetration-test"]
    web = [t for t in app if is_web(t) and not is_thick(t)]
    mk("webapp-unauth", "Web Application Test (Unauthenticated)", "web-app",
       "Web app/API, pre-authentication surface.", [t for t in web if not is_auth(t)],
       lambda t: (next((x for x in t["tags"] if x in WEB_TAGS), "Web")))
    mk("webapp-auth", "Web Application Test (Authenticated)", "web-app",
       "Web app/API with valid session.", [t for t in web if is_auth(t)],
       lambda t: (next((x for x in t["tags"] if x in WEB_TAGS), "Web")))

    cloud = [t for t in tps if t["type"] == "cloud-penetration-test"]
    mk("cloud-assessment", "Cloud Penetration Test", "cloud",
       "Cloud configuration + control-plane review.", cloud,
       lambda t: (t["tags"][0] if t["tags"] else "Cloud"))

    # red-team / vAPT: curated adversarial superset by kill-chain phase, drawn across types
    PHASES = [
        ("1. Reconnaissance & OSINT", lambda t: t["type"] == "external-penetration-test" and re.search(r"osint|subdomain|exposure|recon|enumerate", t["title"], re.I)),
        ("2. Initial Access", lambda t: re.search(r"password|credential|default cred|phish|exploit|administration interface", t["title"], re.I)),
        ("3. Privilege Escalation", lambda t: t["type"] == "host-based-review" and re.search(r"privile|permission|escalat", t["title"], re.I)),
        ("4. Lateral Movement", lambda t: t["type"] == "internal-penetration-test" and re.search(r"segmentation|share|smb|relay|movement|spray", t["title"], re.I)),
        ("5. Persistence & Post-Exploitation", lambda t: "Post Exploitation" in t["tags"] or t["type"] == "red-team" if False else ("Post Exploitation" in t["tags"])),
        ("6. Exfiltration & Impact", lambda t: re.search(r"exfil|egress|data|ransom", t["title"], re.I)),
    ]
    seen = set()
    rt_items = []
    for phase, pred in PHASES:
        for tp in tps:
            key = (tp["type"], tp["title"])
            if key in seen:
                continue
            try:
                ok = pred(tp)
            except Exception:
                ok = False
            if ok:
                seen.add(key)
                rt_items.append(item(tp, phase))
    for i, it in enumerate(rt_items):
        it["order_index"] = i
    out["red-team-vapt"] = {"slug": "red-team-vapt", "name": "Red Team / vAPT (Adversarial)",
                            "kind": "coverage", "category": "red-team",
                            "description": "Longer adversarial-pressure engagement arranged by kill-chain phase; curated across external, internal, and web methodologies.",
                            "items": rt_items}
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--export", required=True)
    ap.add_argument("--out", required=True, help="scribble seed dir")
    args = ap.parse_args()

    rows = load_findings(args.export)
    vulns = build_vulns(rows)
    os.makedirs(args.out, exist_ok=True)
    with open(os.path.join(args.out, "curated_vulnerabilities.json"), "w") as fh:
        json.dump(vulns, fh, indent=1, ensure_ascii=False, sort_keys=True)
    print(f"vulns: {len(rows)} source -> {len(vulns)} merged")

    tps = load_testplans(args.export)
    cls = build_checklists(tps)
    cdir = os.path.join(args.out, "checklists")
    os.makedirs(cdir, exist_ok=True)
    for slug, doc in cls.items():
        with open(os.path.join(cdir, f"{slug}.json"), "w") as fh:
            json.dump(doc, fh, indent=1, ensure_ascii=False)
        print(f"checklist {slug}: {len(doc['items'])} items")
    print(f"test-plan items: {len(tps)}")


if __name__ == "__main__":
    main()
