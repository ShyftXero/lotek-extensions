"""lotek#618 drift guard: the report disposition is derived in ONE place.

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

``test_the_sweep_actually_catches_a_violation`` is the positive control. Without it this file proves
only that it ran — a sweep whose matcher is broken passes silently forever, which is the failure mode
this repo has already been bitten by.
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
