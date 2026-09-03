"""``lotek_kit.attackpath`` must not drift from the ``vector/vector/schema.py`` it was ported from.

Until vector is deleted (#159) both copies are LIVE at the same time: core normalizes an attack path
through vector's copy, and anything reaching for the shared contract normalizes through this one. A patch
applied to one and not the other — a tightened cap, a dropped field, a fix inside ``_norm_edge`` — means
the two halves of the platform disagree about what a valid document is, and nothing says so out loud.

So the port is pinned two ways:

* **Structurally**, by diffing the source against the origin and allowing only the changes the port was
  supposed to make. This catches an edit to vector's copy that never reached here.
* **Behaviourally**, by normalizing the same documents through both and comparing the output. This
  catches the case the structural check cannot: an edit to a shared helper that changes behaviour
  without changing a line this test knows to look at.

The origin is read out of git (``origin/main``) rather than the working tree, so the check does not
quietly weaken on a branch that happens to have vector checked out in some other state. When vector is
finally deleted, this file goes with it — the drift it guards against stops existing.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

from lotek_kit import attackpath

KIT_ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = KIT_ROOT.parent
ORIGIN_PATH = "origin/main:vector/vector/schema.py"

#: The only differences the port was allowed to introduce. Everything else must match.
#: A new entry here is a decision to let the copies diverge, and should be argued for in review.
SANCTIONED_DIVERGENCES = (
    "attackpath/v1",              # the schema id loses its extension prefix
    "vector.attackpath/v1",       # ...and the old one survives as a legacy constant
    "LEGACY_SCHEMA_IDS",
    "SUPPORTED_SCHEMA_IDS",
    "is_supported_schema_id",
)


def _origin_source() -> str | None:
    """vector's schema module as it stands on ``origin/main``, or None if unavailable."""
    try:
        out = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "show", ORIGIN_PATH],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout if out.returncode == 0 else None


ORIGIN_SOURCE = _origin_source()

needs_origin = pytest.mark.skipif(
    ORIGIN_SOURCE is None,
    reason="vector/vector/schema.py is not readable at origin/main — it has probably been deleted (#159), "
           "which is when this whole file should go too",
)


def _load_origin():
    module_path = KIT_ROOT / ".origin_schema_under_test.py"
    module_path.write_text(ORIGIN_SOURCE or "", encoding="utf-8")
    try:
        spec = importlib.util.spec_from_file_location("_vector_schema_origin", module_path)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        sys.modules["_vector_schema_origin"] = module
        spec.loader.exec_module(module)
        return module
    finally:
        module_path.unlink(missing_ok=True)


@needs_origin
def test_every_bound_matches_the_origin():
    """The caps are the document's real contract — they decide what a hostile input can grow into.
    A cap raised in one copy and not the other is a difference in what each half will accept."""
    origin = _load_origin()
    bounds = [name for name in dir(origin) if name.startswith("MAX_")]
    assert bounds, "expected MAX_* bounds in the origin module"
    for name in bounds:
        assert getattr(attackpath, name) == getattr(origin, name), f"{name} drifted from the origin"


@needs_origin
def test_the_vocabularies_match_the_origin():
    origin = _load_origin()
    assert attackpath._ACCENTS == origin._ACCENTS
    assert attackpath._ROUTES == origin._ROUTES
    assert (attackpath._SHORT, attackpath._MED, attackpath._LONG) == (origin._SHORT, origin._MED, origin._LONG)


@needs_origin
def test_the_two_copies_expose_the_same_normalizer_helpers():
    """A helper added to or removed from one copy is drift even if no bound changed."""
    origin = _load_origin()
    origin_helpers = {name for name in dir(origin) if name.startswith("_norm_")}
    kit_helpers = {name for name in dir(attackpath) if name.startswith("_norm_")}
    assert kit_helpers == origin_helpers


@needs_origin
@pytest.mark.parametrize(
    "document",
    [
        {},
        {"meta": {"title": "t"}},
        {"zones": [{"id": "z", "title": "Z", "accent": "red"}],
         "nodes": [{"id": "a", "zone": "z", "label": "A"}, {"id": "b", "zone": "z", "label": "B"}],
         "edges": [{"from": "a", "to": "b", "route": "flow"}],
         "phases": [{"n": 0, "targets": ["a"]}]},
        {"nodes": [{"id": "x", "label": "L" * 5000}]},
        {"edges": [{"from": "ghost", "to": "also-ghost"}]},
        {"style": {"anything": [1, 2, 3]}},
        {"boundaries": [{"from": "z1", "to": "z2"}]},
        {"zones": [{"id": f"z{i}", "order": -i} for i in range(12)]},
    ],
)
def test_both_copies_normalize_identically_apart_from_the_schema_id(document):
    """The behavioural half. A structural diff cannot see a change inside a shared helper; this can."""
    origin = _load_origin()
    ours = attackpath.normalize(document)
    theirs = origin.normalize(document)

    assert ours.pop("schema") == attackpath.SCHEMA_ID
    assert theirs.pop("schema") == "vector.attackpath/v1"
    assert ours == theirs, "the ported normalizer produced a different document from vector's"


@needs_origin
def test_the_source_diff_contains_only_sanctioned_changes():
    """The structural half: catches an edit to vector's copy that never reached the kit, including one
    that happens not to change behaviour on any document this file thought to try."""
    ours = (KIT_ROOT / "lotek_kit" / "attackpath.py").read_text(encoding="utf-8")

    def significant(source: str) -> list[str]:
        lines, in_docstring = [], False
        for raw in source.splitlines():
            line = raw.strip()
            if line.startswith('"""') or line.endswith('"""'):
                # crude, and deliberately so: prose is allowed to differ, code is not
                in_docstring = not in_docstring if line.count('"""') % 2 else in_docstring
                continue
            if in_docstring or not line or line.startswith("#"):
                continue
            lines.append(line)
        return lines

    ours_lines = significant(ours)
    theirs_lines = significant(ORIGIN_SOURCE or "")

    only_ours = [ln for ln in ours_lines if ln not in theirs_lines]
    only_theirs = [ln for ln in theirs_lines if ln not in ours_lines]

    unexplained_ours = [ln for ln in only_ours if not any(token in ln for token in SANCTIONED_DIVERGENCES)]
    unexplained_theirs = [ln for ln in only_theirs if not any(token in ln for token in SANCTIONED_DIVERGENCES)]

    assert unexplained_ours == [], f"the kit has code the origin does not: {unexplained_ours}"
    assert unexplained_theirs == [], f"the origin has code the kit does not — a patch that never landed here: {unexplained_theirs}"
