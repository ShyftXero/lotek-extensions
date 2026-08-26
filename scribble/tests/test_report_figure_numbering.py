"""ext#117 (FEAT-4 residual) — figures are numbered ``Figure N — …``, continuously, and IDENTICALLY in
the HTML and DOCX deliverables.

The original FEAT-4 ask was "auto-numbered figure captions ('Figure 1 — …') referenced from the text".
What shipped was a bare ``<figcaption>`` of prose, so body text had nothing to cross-reference, and the
issue is explicit about the failure mode that matters most: *"a figure that is 'Figure 3' on screen and
unnumbered in Word is worse than neither."*

So the load-bearing test here is :func:`test_html_and_docx_number_the_same_figures_the_same_way`, which
renders BOTH deliverables from ONE context and compares the sequences. Everything else pins the pieces
that make that structurally true rather than coincidental — chiefly that the numbers are assigned once,
in ``context.number_figures``, and that they do NOT depend on whether a given renderer managed to embed
the bytes (which varies with the inlining budget and with whether the caller passed an artifact reader
at all — number off that and the same report gets two different sequences).
"""

from __future__ import annotations

import io
import re

import docx
from docx.oxml.ns import qn

from scribble.content import schema
from scribble.enums import ArtifactKind, ArtifactPlacement, Severity
from scribble.models import (
    Artifact,
    Client,
    Engagement,
    EngagementDiagram,
    EngagementFinding,
    FindingGroup,
)
from scribble.reporting import build_report_context, figure_caption, number_figures
from scribble.reporting.context import ArtifactCtx, DiagramCtx, FindingCtx, GroupCtx
from scribble.reporting.render_docx import render_report_docx
from scribble.reporting.render_html import render_report_html
from tests.test_report_attack_path_docx import snapshot

_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108020000009077"
    "53de0000000c4944415478da6360606000000005000166ff0f0e0000000049454e44ae426082"
)

_FIGURE_RE = re.compile(r"Figure (\d+) — ([A-Za-z0-9 .()/-]+)")


def _block(text: str) -> dict:
    return schema.doc_from_text(text)


def _figures(text: str) -> list[tuple[int, str]]:
    """Every ``Figure N — caption`` in a rendered deliverable, deduped, in numeric order. Deduped
    because the HTML repeats a caption in its lightbox ``alt`` and the DOCX may repeat one in a
    header/footer — what is being compared is the SEQUENCE, not how often each is printed."""
    seen = {int(n): cap.strip() for n, cap in _FIGURE_RE.findall(text)}
    return sorted(seen.items())


def _docx_text(raw: bytes) -> str:
    """Document text, one line PER PARAGRAPH (table cells included — a cell is made of ``w:p``s).

    The paragraph break matters: joining every ``w:t`` run edge-to-edge runs a caption straight into
    the next paragraph's first words, and the caption regex then happily swallows both."""
    doc = docx.Document(io.BytesIO(raw))
    return "\n".join(
        "".join(t.text or "" for t in p.iter(qn("w:t"))) for p in doc.element.body.iter(qn("w:p"))
    )


def _engagement(session_factory, tmp_path):
    """Every figure-bearing surface in one engagement: a finding's own evidence, a NESTED child's
    evidence, an attack-path diagram, and an engagement-level artifact (which has no finding gallery
    and lands in the appendix). Anything less and the "continuous across the report" claim is untested
    where it is most likely to break — at a section boundary."""
    store = tmp_path / "artifacts"
    store.mkdir(exist_ok=True)
    for name in ("alpha.png", "bravo.png", "charlie.png", "delta.png"):
        (store / name).write_bytes(_PNG)
    with session_factory() as db:
        client = Client(name="Acme Co")
        db.add(client)
        db.flush()
        eng = Engagement(name="TeamsPlus Assessment", client_id=client.id, company_name="Acme Corp")
        grp = FindingGroup(engagement=eng, name="External", order_index=0)
        parent = EngagementFinding(
            engagement=eng, group=grp, title="Reflected XSS", severity=Severity.high,
            order_index=0, content_json={"description": _block("Reflected XSS on /search.")},
        )
        db.add(eng)
        db.flush()
        child = EngagementFinding(
            engagement=eng, group=grp, title="Reflected XSS host b", severity=Severity.high,
            order_index=1, parent_id=parent.id,
            content_json={"description": _block("Same issue, host b.")},
        )
        db.add(child)
        db.flush()
        rows = [
            (parent.id, "alpha.png", "Payload firing in the browser", 0),
            (parent.id, "bravo.png", "Burp request and response", 1),
            (child.id, "charlie.png", "Host b payload", 0),
            (None, "delta.png", "Engagement wide network capture", 0),
        ]
        for finding_id, filename, caption, order_index in rows:
            db.add(Artifact(
                engagement_id=eng.id, finding_id=finding_id, kind=ArtifactKind.screenshot,
                placement=ArtifactPlacement.attached, filename=filename, caption=caption,
                content_type="image/png", storage_path=str(store / filename),
                order_index=order_index, byte_size=len(_PNG),
            ))
        db.add(EngagementDiagram(
            engagement_id=eng.id, diagram_ref="ref-0", caption="Domain compromise chain",
            embed_html=snapshot(), order_index=0,
        ))
        db.commit()
        return eng.id, store


def _read(store):
    def _bytes(path: str) -> bytes | None:
        from pathlib import Path

        p = Path(path)
        return p.read_bytes() if p.exists() else None

    return _bytes


# ── the requirement, stated as one assertion ─────────────────────────────────────────────────────────


def test_html_and_docx_number_the_same_figures_the_same_way(session_factory, tmp_path):
    eng_id, store = _engagement(session_factory, tmp_path)
    reader = _read(store)
    with session_factory() as db:
        ctx = build_report_context(db.get(Engagement, eng_id))
        html = render_report_html(ctx, inline_assets=True, artifact_bytes=reader)
        raw = render_report_docx(ctx, artifact_bytes=reader)

    html_figures = _figures(html)
    docx_figures = _figures(_docx_text(raw))
    assert html_figures == docx_figures, f"HTML {html_figures} != DOCX {docx_figures}"
    # …and the sequence is a real one: continuous from 1, one per figure, no gaps and no repeats.
    assert [n for n, _ in html_figures] == [1, 2, 3, 4, 5]
    assert [cap for _, cap in html_figures] == [
        "Payload firing in the browser",
        "Burp request and response",
        "Host b payload",
        "Domain compromise chain",
        "Engagement wide network capture",
    ]


def test_numbering_does_not_depend_on_whether_the_bytes_embedded(session_factory, tmp_path):
    """Embed success varies with the renderer's inlining budget and with whether the caller supplied an
    artifact reader at all. Number off it and one engagement gets two different sequences — the exact
    defect ext#117 names. Rendered here with NO reader: every artifact degrades to a "not embedded"
    chip and the numbers must be unchanged."""
    eng_id, store = _engagement(session_factory, tmp_path)
    with session_factory() as db:
        ctx = build_report_context(db.get(Engagement, eng_id))
        embedded = _figures(render_report_html(ctx, inline_assets=True, artifact_bytes=_read(store)))
        bare = _figures(render_report_html(ctx))
    assert bare == embedded


def test_figures_carry_stable_cross_reference_anchors(session_factory, tmp_path):
    """"Give each figure a stable anchor id so finding body text can cross-reference it." The id is
    derived from the number, so a cross-reference written as ``#fig-3`` lands on Figure 3."""
    eng_id, store = _engagement(session_factory, tmp_path)
    with session_factory() as db:
        ctx = build_report_context(db.get(Engagement, eng_id))
        html = render_report_html(ctx, inline_assets=True, artifact_bytes=_read(store))
    for n in range(1, 6):
        assert f'id="fig-{n}"' in html, f"no anchor for figure {n}"


def test_the_attack_path_diagram_is_numbered_in_the_same_sequence(session_factory, tmp_path):
    """A diagram is a figure. Numbering it in a separate sequence (or not at all) would make "Figure 4"
    ambiguous — the thing #117 says must not happen."""
    eng_id, store = _engagement(session_factory, tmp_path)
    with session_factory() as db:
        ctx = build_report_context(db.get(Engagement, eng_id))
        html = render_report_html(ctx, inline_assets=True, artifact_bytes=_read(store))
    assert "Figure 4 — Domain compromise chain" in html
    assert '<figure class="attack-path-item" id="fig-4">' in html


def test_an_uncaptioned_diagram_is_still_numbered(session_factory, tmp_path):
    """The NUMBER is what body text cross-references, so a blank caption may not skip a figure."""
    eng_id, _store = _engagement(session_factory, tmp_path)
    with session_factory() as db:
        eng = db.get(Engagement, eng_id)
        eng.diagrams[0].caption = ""
        db.commit()
        ctx = build_report_context(db.get(Engagement, eng_id))
        html = render_report_html(ctx)
    assert "Figure 4 — Attack path" in html


# ── the ordering rule itself, isolated from the DB ───────────────────────────────────────────────────


def _artifact(name: str) -> ArtifactCtx:
    return ArtifactCtx(id=name, kind="screenshot", filename=f"{name}.png", caption=name,
                       content_type="image/png", storage_path=f"/tmp/{name}.png")


def _finding(name: str, artifacts, children=()) -> FindingCtx:
    return FindingCtx(id=name, title=name, severity="high", cvss_score=None, cvss_vector=None,
                      target_host=None, target_port=None, target_url=None, blocks_html={},
                      artifacts=list(artifacts), children=list(children))


def test_number_figures_walks_findings_then_diagrams_then_the_appendix():
    """Document order, pinned without a database: a parent's own evidence, then each nested child's,
    then the diagrams, then the engagement-level appendix. That is the order BOTH HTML templates
    (``findings`` -> ``diagrams`` -> ``evidence``) and ``render_report_docx``'s post-render appends
    produce — which is the only reason the two deliverables agree."""
    child = _finding("child", [_artifact("c1")])
    parent = _finding("parent", [_artifact("p1"), _artifact("p2")], children=[child])
    second = _finding("second", [_artifact("s1")])
    groups = [GroupCtx(id=None, name="g", type_slug=None, color=None, findings=[parent, second])]
    diagrams = [DiagramCtx(id="d", diagram_ref="", caption="d", embed_html="")]
    appendix = [_artifact("e1")]

    assert number_figures(groups, diagrams, appendix) == 6
    assert [a.figure_number for a in parent.artifacts] == [1, 2]
    assert [a.figure_number for a in child.artifacts] == [3]
    assert [a.figure_number for a in second.artifacts] == [4]
    assert diagrams[0].figure_number == 5
    assert appendix[0].figure_number == 6


def test_figure_caption_degrades_rather_than_printing_a_dangling_dash():
    assert figure_caption(3, "A capture") == "Figure 3 — A capture"
    assert figure_caption(3, "") == "Figure 3"
    assert figure_caption(None, "A capture") == "A capture"
    assert figure_caption(None, "") == ""
