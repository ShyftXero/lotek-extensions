"""Regression guard for #56 — cream's own pytest config must not force quiet output.

``cream/pyproject.toml`` used to set ``addopts = "-q"``. A caller's habitual ``pytest -q``
then resolved to ``-qq``, at which pytest drops the ``N passed in Xs`` summary line entirely
on a green run — the one signal the PR gate relies on for exact counts. It does not swallow
failures (FAILED lines and a non-zero exit still print), so the defect is scoped to the
green-path summary only. This test pins the config-level fix: cream's own ``addopts`` must
never contain a quiet flag, so a caller's own ``-q`` cannot compound past the count-line
threshold.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

QUIET_TOKENS = {"-q", "-qq", "--quiet"}


def _addopts_tokens() -> list[str]:
    pyproject_path = Path(__file__).resolve().parents[1] / "pyproject.toml"
    with pyproject_path.open("rb") as fh:
        data = tomllib.load(fh)
    addopts = data["tool"]["pytest"]["ini_options"].get("addopts", "")
    if isinstance(addopts, str):
        return addopts.split()
    return list(addopts)


def test_addopts_does_not_force_quiet_output() -> None:
    tokens = _addopts_tokens()
    forced_quiet = QUIET_TOKENS.intersection(tokens)
    assert not forced_quiet, (
        f"cream/pyproject.toml addopts contains {forced_quiet!r} — this compounds with a "
        "caller's own -q into -qq, at which pytest drops the 'N passed in Xs' summary line "
        "on a green run (issue #56). Use a non-compounding default instead (e.g. --tb=short) "
        "or drop addopts entirely."
    )
