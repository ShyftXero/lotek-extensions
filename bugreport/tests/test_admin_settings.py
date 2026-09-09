"""The two ADMIN ``[[settings]]`` knobs — declaration, resolution, and the clamps.

Both knobs size a SECURITY bound: `share_ttl_days` is the lifetime of an unauthenticated bearer
capability URL, and `max_attachment_mb` is a per-file upload ceiling. So the host validating against
the manifest's declared `min`/`max` is not the only check — `deps` re-clamps whatever the host hands
back, and a value it cannot use degrades to the shipped default rather than to "no limit". That
re-clamp is what these tests pin, because it is the half a mounted test cannot see: a stub host proves
logic, never the mount, but the mount cannot prove the clamp either.

Hermetic: a fake `extension_setting` hook injected through `extras`, no host and no database.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from bugreport import deps
from bugreport.models import (
    MAX_ATTACHMENT_BYTES,
    MAX_ATTACHMENT_MB_BOUNDS,
    SHARE_TTL_DAYS,
    SHARE_TTL_DAYS_BOUNDS,
)

MANIFEST = Path(__file__).resolve().parent.parent / "lotek-extension.toml"
DATA = tomllib.loads(MANIFEST.read_text())
SETTINGS = {entry["key"]: entry for entry in DATA.get("settings", [])}


class _Cfg:
    """The mounted-config shape `deps.get_config()` returns — only `extras` matters here."""

    def __init__(self, hook=None):
        self.extras = {} if hook is None else {"extension_setting": hook}


@pytest.fixture()
def hosted(monkeypatch):
    """Install a fake host settings hook and return a setter for the value it answers with."""
    box: dict = {}

    def hook(key, default=None):
        if "raises" in box:
            raise RuntimeError("the host hook blew up")
        return box.get(key, default)

    monkeypatch.setattr(deps, "get_config", lambda: _Cfg(hook))
    return box


# ── the declaration ────────────────────────────────────────────────────────────────────────────


def test_both_knobs_are_declared_with_bounds_matching_the_code():
    """The manifest's `min`/`max` and the module's `_BOUNDS` tuples must agree.

    They are two copies of one decision — the host validates against the manifest, `deps` re-clamps
    against the module — so a drift means either the form accepts a value the code then silently
    rewrites, or the code rejects a value the form said was fine.
    """
    assert set(SETTINGS) == {"share_ttl_days", "max_attachment_mb"}
    ttl, mb = SETTINGS["share_ttl_days"], SETTINGS["max_attachment_mb"]
    assert (ttl["min"], ttl["max"]) == SHARE_TTL_DAYS_BOUNDS
    assert (mb["min"], mb["max"]) == MAX_ATTACHMENT_MB_BOUNDS
    assert ttl["default"] == SHARE_TTL_DAYS
    assert mb["default"] == MAX_ATTACHMENT_BYTES // (1024 * 1024)
    # Neither may be a secret: `secret = true` is only meaningful for a credential, and these are not.
    assert not ttl.get("secret") and not mb.get("secret")


# ── resolution ─────────────────────────────────────────────────────────────────────────────────


def test_standalone_with_no_host_gets_the_shipped_defaults(monkeypatch):
    monkeypatch.setattr(deps, "get_config", lambda: _Cfg())
    assert deps.share_ttl_days() == SHARE_TTL_DAYS
    assert deps.max_attachment_bytes() == MAX_ATTACHMENT_BYTES


def test_a_saved_value_is_honoured(hosted):
    hosted["share_ttl_days"] = 30
    hosted["max_attachment_mb"] = 100
    assert deps.share_ttl_days() == 30
    assert deps.max_attachment_bytes() == 100 * 1024 * 1024


def test_a_throwing_host_hook_degrades_to_the_default(hosted):
    hosted["raises"] = True
    assert deps.share_ttl_days() == SHARE_TTL_DAYS
    assert deps.max_attachment_bytes() == MAX_ATTACHMENT_BYTES


# ── the clamps: every one of these must degrade, never widen ───────────────────────────────────


# The resolvers make a DISTINCTION worth pinning separately, and writing these tests is what forced
# it to be stated: a value that is COERCIBLE but out of range is clamped to the nearest bound, while
# a value that is not usable as a number at all falls back to the shipped default. Both satisfy the
# security property (never wider than the declared ceiling), but they are different behaviours and
# the first draft of this file asserted fallback for both and failed. Clamping is the better of the
# two for an out-of-range number: the realistic way one gets stored is that an operator saved it when
# the bounds were wider, and clamping honours that intent as closely as today's bounds allow, where
# fallback would silently discard it.


@pytest.mark.parametrize(
    "value,expected",
    [
        pytest.param(0, 1, id="zero-clamps-up-to-the-floor-not-to-7"),
        pytest.param(-5, 1, id="negative-clamps-to-the-floor"),
        pytest.param(10_000, 365, id="above-max-clamps-to-the-ceiling"),
    ],
)
def test_an_out_of_range_ttl_is_clamped_to_the_bound(hosted, value, expected):
    """Never zero (which would refuse every share) and never past the ceiling."""
    hosted["share_ttl_days"] = value
    assert deps.share_ttl_days() == expected


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(True, id="bool-is-an-int-in-python"),
        pytest.param(False, id="bool-false"),
        pytest.param("not a number", id="non-numeric-string"),
        pytest.param(None, id="none"),
        pytest.param(object(), id="not-a-scalar"),
        pytest.param([7], id="a-list"),
    ],
)
def test_an_unusable_ttl_falls_back_to_the_default(hosted, value):
    """`True`/`False` are called out because `isinstance(True, int)` is True in Python, so a bool
    reaching an int knob would otherwise silently mean 1 day (or 0)."""
    hosted["share_ttl_days"] = value
    assert deps.share_ttl_days() == SHARE_TTL_DAYS


@pytest.mark.parametrize(
    "value,expected_mb",
    [(0, 1), (-1, 1), (100_000, 200)],
)
def test_an_out_of_range_attachment_cap_is_clamped(hosted, value, expected_mb):
    hosted["max_attachment_mb"] = value
    assert deps.max_attachment_bytes() == expected_mb * 1024 * 1024


@pytest.mark.parametrize("value", [True, "big", None, object()])
def test_an_unusable_attachment_cap_falls_back_to_the_default(hosted, value):
    hosted["max_attachment_mb"] = value
    assert deps.max_attachment_bytes() == MAX_ATTACHMENT_BYTES


def test_no_resolved_value_can_exceed_the_declared_ceiling(hosted):
    """The property that actually matters, stated once over every case above: whatever the host
    returns, neither bound is ever widened past what the manifest declares."""
    ttl_hi = SHARE_TTL_DAYS_BOUNDS[1]
    mb_hi = MAX_ATTACHMENT_MB_BOUNDS[1]
    for value in (10**9, "999999999", 10**9 * 1.0, float("inf")):
        hosted["share_ttl_days"] = value
        hosted["max_attachment_mb"] = value
        assert deps.share_ttl_days() <= ttl_hi, f"{value!r} widened the share TTL"
        assert deps.max_attachment_bytes() <= mb_hi * 1024 * 1024, f"{value!r} widened the cap"


def test_a_numeric_string_at_the_edges_is_accepted(hosted):
    """A form posts strings, so the resolver must coerce rather than reject — but only inside bounds."""
    lo, hi = SHARE_TTL_DAYS_BOUNDS
    hosted["share_ttl_days"] = str(lo)
    assert deps.share_ttl_days() == lo
    hosted["share_ttl_days"] = str(hi)
    assert deps.share_ttl_days() == hi


# ── the wiring: every caller must pass the resolved value ──────────────────────────────────────


def test_every_call_site_passes_the_resolved_value_not_the_constant():
    """A knob nothing reads is a decorative form.

    `service.py` is deliberately Flask-free (it takes these as parameters), so the READ happens in
    `blueprint.py` and `api_pat.py`. This asserts both surfaces pass the resolver at every call —
    miss one and that surface silently keeps the hardcoded bound while the form claims otherwise.
    """
    root = Path(__file__).resolve().parent.parent / "bugreport"
    for module, calls in (
        ("blueprint.py", ("max_bytes=max_attachment_bytes()", "ttl_days=share_ttl_days()")),
        ("api_pat.py", ("max_bytes=max_attachment_bytes()", "ttl_days=share_ttl_days()")),
    ):
        src = (root / module).read_text()
        for call in calls:
            assert call in src, f"{module} does not pass {call}"

    # And no surface still hands the raw module constant to the streaming cap.
    service = (root / "service.py").read_text()
    assert "_CappedHeadReader(head, stream, max_bytes)" in service, (
        "service.attach must enforce the PARAMETER, not the module constant"
    )
