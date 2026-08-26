"""ext#115 (BUG-7) — the attack path must not be SILENTLY DROPPED from the ``.docx`` deliverable.

Reported from the TeamsPlus engagement, retest 2026-08-26: the HTML export embedded the diagram as an
animated ``<iframe class="attack-path-frame">`` and played it, while the ``.docx`` rendered from the
SAME engagement contained no attack path at all — the string ``Attack path`` did not appear in
``word/document.xml`` and all 8 ``<w:drawing>`` elements were evidence screenshots. A reader given only
the Word deliverable had no way to know a diagram existed.

Why these assertions look the way they do
-----------------------------------------
This is a "renders fine, content missing" defect, so a test that only proves the render SUCCEEDED is
worthless — that test was already green throughout the bug. Every assertion below therefore names
CONTENT that must be present in the rendered document (the zone titles, the host labels, the phase
walkthrough, the caption), read back out of the real ``.docx`` rather than off the context.

``tests/test_report_attack_path.py`` covers the HTML side and the backward-compat guarantee; this file
is its ``.docx`` half plus the untrusted-snapshot bounds.
"""

from __future__ import annotations

import io
import json
import zipfile

import docx
from docx.oxml.ns import qn

from scribble.content import schema
from scribble.enums import Severity
from scribble.models import Client, Engagement, EngagementDiagram, EngagementFinding, FindingGroup
from scribble.reporting import build_report_context
from scribble.reporting.render_docx import render_report_docx

MODEL = {
    "schema": "vector.attackpath/v1",
    "meta": {"title": "Acme compromise chain", "subtitle": "external to domain admin"},
    "zones": [
        {"id": "ext", "title": "Internet", "order": 0},
        {"id": "dmz", "title": "DMZ", "order": 1},
        {"id": "core", "title": "Core AD", "order": 2},
    ],
    "nodes": [
        {"id": "kali", "label": "Operator", "zone": "ext", "row": 0, "ip": "203.0.113.9"},
        {"id": "web", "label": "www01", "zone": "dmz", "row": 0, "ip": "198.51.100.5",
         "states": [{"at": 1, "state": "owned"}]},
        {"id": "dc", "label": "DC01", "zone": "core", "row": 1, "domain": "acme.local",
         "states": [{"at": 1, "state": "target"}, {"at": 3, "state": "owned"}]},
    ],
    "edges": [
        {"id": "e1", "from": "kali", "to": "web", "kind": "attack", "at": 1, "label": "Struts RCE"},
        {"id": "e2", "from": "web", "to": "dc", "kind": "attack", "at": 3, "label": "Kerberoast"},
        # dangling: vector drops these and so must this renderer — never draw a line to nowhere.
        {"id": "e3", "from": "web", "to": "ghost", "kind": "attack", "at": 3},
    ],
    "phases": [
        {"n": 0, "intro": True, "title": "Setup"},
        {"n": 1, "title": "Initial access", "mitre": "T1190", "desc": "Exploit the edge web app."},
        {"n": 3, "title": "Domain compromise", "mitre": "T1558.003", "desc": "Kerberoast to DA."},
    ],
}


def _script_json(obj) -> str:
    """vector's ``render.json_for_script`` escaping: ``<``/``>``/``&`` become ``\\uXXXX``, which is what
    makes "match to the first ``</script>``" exact rather than best-effort."""
    esc = {ord("<"): "\\u003c", ord(">"): "\\u003e", ord("&"): "\\u0026"}
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).translate(esc)


def snapshot(model=None) -> str:
    """A stand-in for vector's ``export.html`` — the same shape ``templates/vector/deliverable.html.j2``
    emits, including the ``<script type="application/json" id="vap-model">`` block this renderer reads."""
    return (
        "<!doctype html><html><head><title>Acme compromise chain</title></head><body>"
        '<div id="vap"></div>'
        f'<script type="application/json" id="vap-model">{_script_json(model or MODEL)}</script>'
        "<script>/* viewer runtime */</script></body></html>"
    )


def _block(text: str) -> dict:
    return schema.doc_from_text(text)


def _engagement(session_factory, *, diagrams: list[tuple[str, str]] | None = None):
    """``diagrams`` is a list of ``(caption, embed_html)``; ``None`` links none."""
    with session_factory() as db:
        client = Client(name="Acme Co")
        db.add(client)
        db.flush()
        eng = Engagement(name="TeamsPlus Assessment", client_id=client.id, company_name="Acme Corp")
        grp = FindingGroup(engagement=eng, name="External", order_index=0)
        EngagementFinding(
            engagement=eng, group=grp, title="Reflected XSS", severity=Severity.high,
            order_index=0, content_json={"description": _block("Reflected XSS on /search.")},
        )
        db.add(eng)
        db.flush()
        for i, (caption, embed) in enumerate(diagrams or []):
            db.add(EngagementDiagram(
                engagement_id=eng.id, diagram_ref=f"ref-{i}", caption=caption,
                embed_html=embed, order_index=i,
            ))
        db.commit()
        return eng.id


def _render(session_factory, eng_id) -> bytes:
    with session_factory() as db:
        eng = db.get(Engagement, eng_id)
        return render_report_docx(build_report_context(eng))


def _text(raw: bytes) -> str:
    doc = docx.Document(io.BytesIO(raw))
    return "".join(t.text or "" for t in doc.element.body.iter(qn("w:t")))


def _document_xml(raw: bytes) -> str:
    return zipfile.ZipFile(io.BytesIO(raw)).read("word/document.xml").decode("utf-8")


# ── the reported defect, asserted as the reporter measured it ────────────────────────────────────────


def test_docx_carries_the_attack_path_the_html_has(session_factory):
    """The headline of ext#115, phrased exactly as the bug report measured it: `grep 'Attack path'`
    over ``word/document.xml``."""
    eng_id = _engagement(session_factory, diagrams=[("Domain compromise chain", snapshot())])
    raw = _render(session_factory, eng_id)
    xml = _document_xml(raw)
    assert "Attack Paths" in xml
    assert "attack path" in xml.lower()


def test_docx_carries_the_diagram_caption_beneath_the_figure(session_factory):
    """"…with the caption beneath it" (ext#115). Order matters: the caption is a CAPTION, not a title,
    so it must follow the rendition rather than lead it."""
    eng_id = _engagement(session_factory, diagrams=[("Domain compromise chain", snapshot())])
    doc = docx.Document(io.BytesIO(_render(session_factory, eng_id)))
    texts = [p.text for p in doc.paragraphs]
    heading = texts.index("Domain compromise chain")
    caption = next(i for i, t in enumerate(texts) if t.startswith("Figure ") and "Domain" in t)
    assert caption > heading
    # …and after the walkthrough, i.e. beneath the whole figure rather than tucked under its title.
    assert caption > texts.index("Walkthrough")


def test_docx_carries_the_diagram_topology_and_walkthrough(session_factory):
    """The static frame itself: zones as columns, hosts placed in them, the phase narrative, and the
    connections resolved to LABELS. Each of these is content the Word reader silently lost before."""
    eng_id = _engagement(session_factory, diagrams=[("Domain compromise chain", snapshot())])
    text = _text(_render(session_factory, eng_id))
    for zone in ("Internet", "DMZ", "Core AD"):
        assert zone in text, f"zone {zone!r} missing from the docx"
    for host in ("Operator", "www01", "DC01"):
        assert host in text, f"host {host!r} missing from the docx"
    assert "203.0.113.9" in text and "acme.local" in text
    assert "[OWNED]" in text  # the FINAL state, not the first: a still frame shows the end of the path
    assert "Phase 1 — Initial access" in text and "T1190" in text
    assert "Exploit the edge web app." in text
    assert "Operator → www01" in text and "Struts RCE" in text
    assert "ghost" not in text  # the dangling edge is dropped, not drawn to nowhere


def test_docx_names_the_subtitle_and_falls_back_to_the_model_title(session_factory):
    """An operator who linked a diagram without typing a caption still gets a titled figure — the
    model's own ``meta.title`` — rather than an anonymous table."""
    eng_id = _engagement(session_factory, diagrams=[("", snapshot())])
    text = _text(_render(session_factory, eng_id))
    assert "Acme compromise chain" in text
    assert "external to domain admin" in text


# ── backward compatibility: an engagement with no diagram is untouched ───────────────────────────────


def test_no_diagram_appends_no_attack_paths_section(session_factory):
    """The same guarantee ``render_html._render_diagrams`` makes on the HTML side. Without it, every
    existing report would grow an empty section."""
    eng_id = _engagement(session_factory, diagrams=None)
    xml = _document_xml(_render(session_factory, eng_id))
    assert "Attack Paths" not in xml
    assert "Walkthrough" not in xml


# ── an unreadable snapshot degrades LOUDLY, never silently ───────────────────────────────────────────


def test_an_unreadable_snapshot_still_announces_the_diagram(session_factory):
    """``embed_html`` is stored verbatim from a PAT POST, so nothing guarantees it came from vector.
    When the model cannot be read the section must still say the diagram EXISTS — reverting to silence
    is the whole defect."""
    eng_id = _engagement(session_factory, diagrams=[("Chain of pwnage", "<html>not vector</html>")])
    text = _text(_render(session_factory, eng_id))
    assert "Attack Paths" in text
    assert "Chain of pwnage" in text
    assert "interactive figure in the HTML report" in text


def test_malformed_json_in_the_model_block_degrades_gracefully(session_factory):
    bad = '<html><body><script type="application/json" id="vap-model">{not json</script></body></html>'
    eng_id = _engagement(session_factory, diagrams=[("Broken", bad)])
    text = _text(_render(session_factory, eng_id))
    assert "Broken" in text and "interactive figure in the HTML report" in text


def test_a_model_with_no_zones_still_renders_its_walkthrough(session_factory):
    """Nothing placeable to draw, but the phases are still the most useful half of an attack path."""
    model = {"meta": {"title": "Narrative only"}, "zones": [], "nodes": [], "edges": [],
             "phases": [{"n": 1, "title": "Initial access", "desc": "Phished a user."}]}
    eng_id = _engagement(session_factory, diagrams=[("Narrative", snapshot(model))])
    text = _text(_render(session_factory, eng_id))
    assert "Phase 1 — Initial access" in text and "Phished a user." in text


# ── bounds on an untrusted snapshot ──────────────────────────────────────────────────────────────────


def test_a_hostile_snapshot_cannot_render_an_unbounded_table(session_factory):
    """``embed_html`` is operator/agent-supplied and vector's own caps do not apply to it (nothing
    proves it came from vector). Cap zones/rows here or one POST turns every report render into a
    thousand-column table."""
    from scribble.reporting.render_docx import _MAX_DIAGRAM_ZONES

    model = {
        "meta": {"title": "Flood"},
        "zones": [{"id": f"z{i}", "title": f"Zone {i}", "order": i} for i in range(400)],
        "nodes": [{"id": f"n{i}", "zone": f"z{i}", "label": f"host{i}", "row": i} for i in range(400)],
        "edges": [], "phases": [],
    }
    eng_id = _engagement(session_factory, diagrams=[("Flood", snapshot(model))])
    doc = docx.Document(io.BytesIO(_render(session_factory, eng_id)))
    assert doc.tables, "expected the zone grid to render at all"
    grid = doc.tables[-1]
    assert len(grid.columns) == _MAX_DIAGRAM_ZONES
    assert len(grid.rows) <= 41  # header + _MAX_DIAGRAM_ROWS


def test_an_oversized_snapshot_is_not_scanned(session_factory):
    """A snapshot past the scan bound is not regex-walked at all — it degrades to the honest note
    rather than costing a multi-megabyte backtracking scan on every report render."""
    from scribble.reporting.render_docx import _MAX_DIAGRAM_SCAN_BYTES

    huge = "<html>" + ("x" * (_MAX_DIAGRAM_SCAN_BYTES + 1)) + "</html>"
    eng_id = _engagement(session_factory, diagrams=[("Huge", huge)])
    text = _text(_render(session_factory, eng_id))
    assert "Huge" in text and "interactive figure in the HTML report" in text


def test_untrusted_model_fields_are_coerced_not_trusted(session_factory):
    """Every field is coerced/capped on the way out: a dict where a label belongs, a 10k-character
    description, a bool where a number belongs. None of these may reach python-docx as-is."""
    model = {
        "meta": {"title": {"nope": 1}, "subtitle": ["also nope"]},
        "zones": [{"id": "z", "title": "Z" * 5000, "order": "not-an-int"}],
        "nodes": [{"id": "n", "zone": "z", "label": {"x": 1}, "row": True, "states": "not-a-list"}],
        "edges": [{"id": "e", "from": "n", "to": "n", "label": 12345}],
        "phases": [{"n": "x", "title": None, "desc": "d" * 9000}],
    }
    eng_id = _engagement(session_factory, diagrams=[("Coerced", snapshot(model))])
    text = _text(_render(session_factory, eng_id))
    assert "Coerced" in text
    assert "Z" * 401 not in text  # the 5000-char zone title was capped
    assert "d" * 1201 not in text
