"""lotek#618 drift guard: the report disposition — and the inclusion rule built on it — are derived in
ONE place each.

``scribble.enums.report_disposition`` / ``finding_status_label`` / ``counts_toward_risk`` are the only
sanctioned readers of what a ``FindingStatus`` *means*. A second copy does not stay equal, and nothing
raises when the copies disagree — you find out from a report whose banner contradicts its own finding
cards. So this sweeps the package's AST for a second computation instead of trusting the convention.

Two shapes are caught:

1. ``FindingStatus.<member>`` attribute access (e.g. ``FindingStatus.false_positive``) — reaching for a
   specific status outside the predicate is how a branch on it starts.
2. a comparison against one of the status VALUE strings (``"false_positive"``, ``"accepted_risk"``, …) —
   the same branch, written with the enum spelled out as a literal to dodge rule 1.

Constructing the enum (``FindingStatus(value)``) or enumerating it (``list(FindingStatus)``) is NOT a
derivation and is deliberately not flagged: the form editor and the machine API legitimately do both.

A THIRD shape is swept further down, for the predicate that consumes this one
(``enums.report_visible`` = ``include_in_report AND disposition != "excluded"``): a raw read of
``.include_in_report`` outside an allowlisted group/artifact/checklist use. That rule exists because the
fork it catches actually shipped — see the comment above ``INCLUSION_ALLOWLIST``.

``test_the_sweep_actually_catches_a_violation`` and ``test_the_inclusion_sweep_actually_catches_a_violation``
are the positive controls. Without them this file proves only that it ran — a sweep whose matcher is
broken passes silently forever, which is the failure mode this repo has already been bitten by.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from scribble.enums import FindingStatus

PACKAGE = pathlib.Path(__file__).resolve().parent.parent / "scribble"

# Every exemption records WHY it is not a status derivation.
#
# Exemptions are per EXPRESSION, not per file: allowlisting a whole module would exempt the next
# derivation someone adds to it, which is how an allowlist quietly becomes the rule.
WHOLE_FILE_ALLOWLIST = {
    # THE home: the predicate, the label map and the disposition constants live here.
    "enums.py",
}

EXPRESSION_ALLOWLIST = {
    # The ORM column default — declaring the initial value of a new row, not interpreting a status.
    ("models.py", "FindingStatus.new"),
}

STATUS_VALUES = {s.value for s in FindingStatus}


class _Sweep(ast.NodeVisitor):
    def __init__(self) -> None:
        self.hits: list[tuple[int, str]] = []

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if isinstance(node.value, ast.Name) and node.value.id == "FindingStatus":
            self.hits.append((node.lineno, f"FindingStatus.{node.attr}"))
        self.generic_visit(node)

    def visit_Compare(self, node: ast.Compare) -> None:
        for side in [node.left, *node.comparators]:
            if isinstance(side, ast.Constant) and side.value in STATUS_VALUES:
                self.hits.append((node.lineno, f"comparison against {side.value!r}"))
        self.generic_visit(node)


def _sweep(source: str) -> list[tuple[int, str]]:
    sweep = _Sweep()
    sweep.visit(ast.parse(source))
    return sweep.hits


def _package_files() -> list[pathlib.Path]:
    return sorted(p for p in PACKAGE.rglob("*.py") if "__pycache__" not in p.parts)


def test_no_second_computation_of_the_report_disposition():
    offenders: list[str] = []
    for path in _package_files():
        if path.name in WHOLE_FILE_ALLOWLIST:
            continue
        for lineno, what in _sweep(path.read_text()):
            if (path.name, what) in EXPRESSION_ALLOWLIST:
                continue
            offenders.append(f"{path.relative_to(PACKAGE)}:{lineno}: {what}")

    assert not offenders, (
        "a status derivation appeared outside scribble/enums.py — call report_disposition() / "
        "finding_status_label() instead, or add a reasoned entry to ALLOWLIST:\n  "
        + "\n  ".join(offenders)
    )


def test_the_sweep_actually_catches_a_violation():
    """Positive control: the matcher must fire on both shapes it claims to catch."""
    member_access = "def f(x):\n    return x.status == FindingStatus.false_positive\n"
    literal_compare = "def f(x):\n    return x.status == 'accepted_risk'\n"

    assert _sweep(member_access), "rule 1 (FindingStatus.<member>) does not fire"
    assert _sweep(literal_compare), "rule 2 (status value literal) does not fire"

    # …and must NOT fire on the legitimate constructor/enumeration shapes the allowlist relies on.
    assert not _sweep("def f(v):\n    return FindingStatus(v)\n")
    assert not _sweep("def f():\n    return list(FindingStatus)\n")


@pytest.mark.parametrize("status", list(FindingStatus))
def test_every_status_has_a_disposition_and_a_label(status):
    """A new ``FindingStatus`` member must not fall through to a default: the maps are exhaustive, so
    adding one without deciding what it means for the deliverable fails here with a KeyError."""
    from scribble.enums import DISPOSITIONS, finding_status_label, report_disposition

    assert report_disposition(status) in DISPOSITIONS
    assert isinstance(finding_status_label(status), str)


# ══ rule 3: the INCLUSION predicate has one home too (lotek#618 follow-up) ══════════════════════════
#
# `report_disposition` above is only half of what decides a deliverable's contents. The other half is
# `enums.report_visible` -- ``include_in_report AND disposition != "excluded"`` -- and it forked the
# moment it was written: `findings_service.rendered_top_level_count`, whose number `api_pat` publishes
# as `top_level_count` ("what the client sees"), still filtered on `include_in_report` ALONE. An
# engagement with one live finding and one false positive rendered ONE card and reported TWO. The
# parity test could not see it because its board carried no statuses, so the convention ("everyone
# calls the predicate") was enforcing nothing.
#
# So sweep for the raw attribute instead of trusting the convention. Every READ of
# `.include_in_report` in the package must be either the predicate itself or an allowlisted entry
# saying which LEVEL it is about -- a group, an artifact, a diagram, a checklist item, or a plain
# serialization of the column. A finding-level read that is not `report_visible` is the bug returning.
#
# Keyed by (path, ENCLOSING FUNCTION, expression) rather than by line number, which churns, or by file,
# which would exempt the next filter someone adds to an already-listed module.
INCLUSION_ALLOWLIST = {
    # ── THE home: the one place the two halves are ANDed. ────────────────────────────────────────
    ("enums.py", "report_visible", "finding.include_in_report"),

    # ── GROUP level. A group is a report section; it has no status and therefore no disposition. ──
    ("reporting/context.py", "build_report_context", "group.include_in_report"),
    ("findings_service.py", "rendered_top_level_count", "group.include_in_report"),
    ("engagement_ui.py", "update_group", "group.include_in_report"),
    ("api_pat.py", "_group_summary", "group.include_in_report"),

    # ── ARTIFACT / DIAGRAM / CHECKLIST level. Same: no `status` column, so nothing to AND with. ───
    ("reporting/context.py", "_artifact_ctxs", "a.include_in_report"),
    ("reporting/context.py", "_diagram_ctxs", "d.include_in_report"),
    ("reporting/context.py", "_build_checklists", "ec.include_in_report"),
    ("reporting/context.py", "_build_activity_log", "getattr(a, 'include_in_report', True)"),
    ("reporting/context.py", "_build_activity_log", "getattr(d, 'include_in_report', True)"),
    ("artifacts_api.py", "_artifact_dict", "a.include_in_report"),
    ("checklists_api.py", "_checklist_out", "ec.include_in_report"),
    ("api_pat.py", "_machine_artifact_dict", "a.include_in_report"),
    ("api_pat.py", "_artifact_summary", "artifact.include_in_report"),
    ("api_pat.py", "_diagram_dict", "d.include_in_report"),
    ("api_pat.py", "scribble_upload_artifact", "existing.include_in_report"),
    ("api_pat.py", "scribble_upload_artifact", "artifact.include_in_report"),
    ("api_pat.py", "scribble_update_artifact", "artifact.include_in_report"),

    # ── Reporting the COLUMN, not deciding inclusion. `_finding_summary` serialises the operator's
    # tick beside the finding's `status`, so a caller can apply the predicate itself; the board draws
    # its "(excluded)" marker from exactly this flag. Neither claims to be "in the deliverable" --
    # `top_level_count` is the field that makes that claim, and it goes through `report_visible`.
    ("api_pat.py", "_finding_summary", "finding.include_in_report"),

    # ── The disposition ROLLUP, which is a different question. "How do the operator's kept findings
    # split across dispositions" needs the veto WITHOUT the disposition half -- ANDing `report_visible`
    # in would make the `excluded` count structurally zero, and "3 excluded" is the number an operator
    # reads to know the report's shape (lotek#633).
    ("reporting/context.py", "_tally_dispositions", "f.include_in_report"),
}


class _InclusionSweep(ast.NodeVisitor):
    """Reads of ``.include_in_report``, tagged with the function they sit in.

    Both spellings: the attribute (``f.include_in_report``) and the defensive
    ``getattr(f, "include_in_report", True)`` the activity appendix uses. Writes (``x.include_in_report
    = …``) and keyword arguments (``include_in_report=True``) are SET operations — a route recording an
    operator's choice, not a second opinion on what the report contains — so they are not swept.
    """

    def __init__(self) -> None:
        self.scope: list[str] = ["<module>"]
        self.hits: list[tuple[int, str, str]] = []

    def visit_FunctionDef(self, node) -> None:
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if node.attr == "include_in_report" and isinstance(node.ctx, ast.Load):
            self.hits.append((node.lineno, self.scope[-1], ast.unparse(node)))
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        if (
            isinstance(node.func, ast.Name)
            and node.func.id == "getattr"
            and len(node.args) >= 2
            and isinstance(node.args[1], ast.Constant)
            and node.args[1].value == "include_in_report"
        ):
            self.hits.append((node.lineno, self.scope[-1], ast.unparse(node)))
        self.generic_visit(node)


def _sweep_inclusion(source: str) -> list[tuple[int, str, str]]:
    sweep = _InclusionSweep()
    sweep.visit(ast.parse(source))
    return sweep.hits


def test_no_second_computation_of_report_visibility():
    offenders: list[str] = []
    for path in _package_files():
        rel = path.relative_to(PACKAGE).as_posix()
        for lineno, func, what in _sweep_inclusion(path.read_text()):
            if (rel, func, what) in INCLUSION_ALLOWLIST:
                continue
            offenders.append(f"{rel}:{lineno} in {func}(): {what}")

    assert not offenders, (
        "a raw `include_in_report` read appeared outside `enums.report_visible`. If it decides whether "
        "a FINDING reaches the deliverable, call report_visible() — filtering on the veto alone is the "
        "lotek#618 `top_level_count` bug. If it is about a group/artifact/diagram/checklist (no status, "
        "so no disposition), add a reasoned entry to INCLUSION_ALLOWLIST:\n  " + "\n  ".join(offenders)
    )


def test_the_inclusion_sweep_actually_catches_a_violation():
    """Positive control: a re-derived finding filter must be seen, in either spelling, and the
    allowlist key must be tight enough that MOVING one into another function still trips."""
    forked = "def rendered_top_level_count(e):\n    return [f for f in e.findings if f.include_in_report]\n"
    defensive = (
        "def visible(e):\n    return [f for f in e.findings if getattr(f, 'include_in_report', True)]\n"
    )

    assert _sweep_inclusion(forked) == [(2, "rendered_top_level_count", "f.include_in_report")]
    assert _sweep_inclusion(defensive) == [
        (2, "visible", "getattr(f, 'include_in_report', True)")
    ]

    # An allowlisted expression relocated into a different function is NOT still allowlisted.
    moved = ("reporting/context.py", "_build_the_new_thing", "group.include_in_report")
    assert moved not in INCLUSION_ALLOWLIST

    # …and a WRITE is not a derivation: recording the operator's tick must not be swept.
    assert not _sweep_inclusion("def set(a, v):\n    a.include_in_report = bool(v)\n")
    assert not _sweep_inclusion("def make(m):\n    return m(include_in_report=True)\n")
