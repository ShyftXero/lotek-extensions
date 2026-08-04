"""Engagement-checklist logic: recommended statuses, rollup buckets, markdown/JSON import/export, and
the copy-on-assign snapshot.

Checklists are NON-BLOCKING visual reminders (see plans/FRACTION_CHECKLISTS.md in lotek). Item status is
a free-text string; the UI offers the kind's recommended values as a dropdown but accepts a custom label,
so anything here that reasons about status does so through the BUCKET mapping, never an exhaustive enum.
"""

from __future__ import annotations

import re
from typing import Any

from fraction.enums import ChecklistKind

# --------------------------------------------------------------------------- status vocabulary

# Recommended per-kind status values. The FIRST value is the per-kind default on assign / new item.
RECOMMENDED_STATUS: dict[ChecklistKind, list[str]] = {
    ChecklistKind.coverage: ["pending", "in_progress", "pass", "fail", "na"],
    ChecklistKind.reminder: ["pending", "done", "blocked", "na"],
    ChecklistKind.compliance: ["pending", "pass", "fail", "na"],
}

# Rollup buckets for report math. A value not listed here (a custom label) buckets to "open".
BUCKETS = ("satisfied", "deficient", "not_applicable", "open")
_STATUS_BUCKET: dict[str, str] = {
    "pass": "satisfied",
    "done": "satisfied",
    "fail": "deficient",
    "blocked": "deficient",
    "na": "not_applicable",
    "n/a": "not_applicable",
    "pending": "open",
    "in_progress": "open",
    "in progress": "open",
    "": "open",
}

BUCKET_LABEL = {
    "satisfied": "Satisfied",
    "deficient": "Deficient",
    "not_applicable": "Not Applicable",
    "open": "Open",
}


def default_status(kind: ChecklistKind) -> str:
    return RECOMMENDED_STATUS.get(kind, ["pending"])[0]


def status_bucket(status: str | None) -> str:
    """Map a (possibly custom) status label to one of BUCKETS. Unknown -> 'open'."""
    return _STATUS_BUCKET.get((status or "").strip().lower(), "open")


def rollup(items: list) -> dict[str, int]:
    """Count items per bucket. ``items`` may be ORM rows or dicts, anything with a ``status``."""
    counts = {b: 0 for b in BUCKETS}
    for it in items:
        status = it["status"] if isinstance(it, dict) else getattr(it, "status", None)
        counts[status_bucket(status)] += 1
    return counts


# --------------------------------------------------------------------------- markdown import/export

_H1 = re.compile(r"^#\s+(.*\S)\s*$")
_H2 = re.compile(r"^#{2,3}\s+(.*\S)\s*$")
_ITALIC = re.compile(r"^\*(.+?)\*\s*$")
# A list item: "- [ ] **Label**: guidance", "- [x] Label", or a bare "- Label" (checkbox optional so a
# non-checkbox list isn't silently dropped on import). The required space after the bullet marker keeps a
# "*italic*" description line from being mistaken for an item.
_ITEM = re.compile(r"^\s*[-*]\s+(?:\[[ xX]?\]\s*)?(.*\S)\s*$")
_LABEL_GUIDANCE = re.compile(r"^\*\*(.+?)\*\*\s*:?\s*(.*)$")


def parse_markdown(text: str) -> dict[str, Any]:
    """Parse a markdown checklist into a template dict.

    ``# Title`` -> name; a following ``*italic*`` line -> description; ``## Section`` -> item section;
    ``- [ ] **Label**: guidance`` -> an item (``text`` = label, ``guidance`` = the rest). A bare
    ``- [ ] text`` becomes an item with no guidance. Checkbox state is ignored on import. Markdown carries
    no kind/framework/control_ref; those default (kind=coverage) and are set later in the editor.
    """
    name = ""
    description = None
    section: str | None = None
    items: list[dict[str, Any]] = []
    order = 0
    saw_title = False
    for raw in (text or "").splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        m = _H2.match(line)
        if m:
            section = m.group(1).strip()
            continue
        m = _H1.match(line)
        if m:
            if not saw_title:
                name = m.group(1).strip()
                saw_title = True
            else:
                section = m.group(1).strip()  # a second H1 acts as a section
            continue
        m = _ITEM.match(line)
        if m:
            body = m.group(1).strip()
            lg = _LABEL_GUIDANCE.match(body)
            if lg:
                item_text = lg.group(1).strip()
                guidance = lg.group(2).strip() or None
            else:
                item_text, guidance = body, None
            items.append(
                {
                    "order_index": order,
                    "section": section,
                    "text": item_text[:512],
                    "guidance": guidance,
                    "framework": None,
                    "control_ref": None,
                    "default_status": None,
                }
            )
            order += 1
            continue
        m = _ITALIC.match(line)
        if m and saw_title and description is None and not items:
            description = m.group(1).strip()
    return {
        "name": name or "Imported checklist",
        "description": description,
        "kind": ChecklistKind.coverage.value,
        "category": None,
        "items": items,
    }


def to_markdown(data: dict[str, Any]) -> str:
    """Render a template dict (see ``template_to_dict``) back to markdown. Lossy: kind, framework, and
    control_ref have no markdown representation (use JSON export for a lossless round-trip)."""
    out: list[str] = [f"# {data.get('name', 'Checklist')}"]
    if data.get("description"):
        out.append("")
        out.append(f"*{data['description']}*")
    last_section = object()
    for it in data.get("items", []):
        sec = it.get("section")
        if sec != last_section:
            out.append("")
            if sec:
                out.append(f"## {sec}")
            last_section = sec
        text = it.get("text", "")
        guidance = it.get("guidance")
        line = f"- [ ] **{text}**" + (f": {guidance}" if guidance else "")
        out.append(line)
    return "\n".join(out) + "\n"


# --------------------------------------------------------------------------- JSON (lossless) dict form

_ITEM_KEYS = ("order_index", "section", "text", "guidance", "framework", "control_ref", "default_status")


def template_to_dict(template) -> dict[str, Any]:
    """Lossless dict for JSON export (kind, framework, control_ref, custom statuses all preserved)."""
    return {
        "slug": template.slug,
        "name": template.name,
        "description": template.description,
        "kind": (template.kind.value if hasattr(template.kind, "value") else template.kind),
        "category": template.category,
        "items": [
            {k: getattr(it, k) for k in _ITEM_KEYS} for it in template.items
        ],
    }


def normalize_template_dict(data: dict[str, Any]) -> dict[str, Any]:
    """Validate/normalize an import dict (from JSON or ``parse_markdown``) to a safe, consistent shape.
    Coerces an unknown kind to coverage; clamps/orders items; drops empty-text items."""
    kind = data.get("kind", "coverage")
    try:
        kind = ChecklistKind(kind).value
    except ValueError:
        kind = ChecklistKind.coverage.value
    items = []
    for i, it in enumerate(data.get("items", []) or []):
        text = (it.get("text") or "").strip()
        if not text:
            continue
        items.append(
            {
                "order_index": i,
                "section": (it.get("section") or None),
                "text": text[:512],
                "guidance": (it.get("guidance") or None),
                "framework": (it.get("framework") or None),
                "control_ref": (it.get("control_ref") or None),
                "default_status": (it.get("default_status") or None),
            }
        )
    return {
        "slug": (data.get("slug") or None),
        "name": (data.get("name") or "Imported checklist").strip()[:255],
        "description": (data.get("description") or None),
        "kind": kind,
        "category": (data.get("category") or None),
        "items": items,
    }


# --------------------------------------------------------------------------- assign (copy-on-assign)


def assign_template(session, engagement, template, *, assigned_by: str | None = None):
    """Snapshot ``template`` onto ``engagement`` as a new EngagementChecklist + items. Copy-on-assign:
    later edits to the template never touch this assignment. Returns the new EngagementChecklist (added
    to the session, not committed)."""
    from fraction.models import EngagementChecklist, EngagementChecklistItem

    kind = template.kind if isinstance(template.kind, ChecklistKind) else ChecklistKind(template.kind)
    n_existing = len(getattr(engagement, "checklists", []) or [])
    ec = EngagementChecklist(
        template_id=template.id,
        name=template.name,
        kind=kind,
        include_in_report=(kind != ChecklistKind.reminder),
        order_index=n_existing,
        assigned_by=assigned_by,
    )
    # Attach via the relationship (not a bare engagement_id) so engagement.checklists stays in sync and
    # cascade-delete of the engagement removes its checklists.
    ec.engagement = engagement
    for it in template.items:
        ec.items.append(
            EngagementChecklistItem(
                order_index=it.order_index,
                section=it.section,
                text=it.text,
                guidance=it.guidance,
                framework=it.framework,
                control_ref=it.control_ref,
                status=(it.default_status or default_status(kind)),
            )
        )
    session.add(ec)
    return ec
