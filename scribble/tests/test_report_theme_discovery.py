"""Discovery of installed report Themes (#103) — the ``scribble.report_themes`` entry point.

Covers ``scribble.reporting.theme_discovery``:

- a discovered Theme is tagged ``provenance == PROVENANCE_INSTALLED``,
- a broken entry point (raising loader, non-callable target, unnamed entry) is collected as an error
  and does not stop the rest of discovery,
- a PLANTED positive control proving the error is actually CAPTURED, not merely "the theme didn't
  appear" (a swallow-the-exception regression would still pass a weaker "not in themes" assertion),
- collision with a bundled name: bundled wins, excluded from ``themes``, and reported in ``collisions``,
- collision between two installed entries: first wins, the rest collide,
- an empty environment yields an empty (not erroring) result,
- the real ``importlib.metadata.entry_points`` default path is wired to the right group name.

Fakes entry points by hand (objects carrying ``.name``, ``.load()``, ``.dist``) — no package is ever
actually installed.
"""

from __future__ import annotations

import importlib.metadata

from scribble.reporting import theme_discovery
from scribble.reporting.theme_discovery import (
    PROVENANCE_INSTALLED,
    REPORT_THEME_ENTRY_POINT_GROUP,
    ThemeCollision,
    ThemeLoadError,
    discover_installed_themes,
)


class _FakeDist:
    def __init__(self, name: str) -> None:
        self.name = name


class _FakeEntryPoint:
    """Stands in for ``importlib.metadata.EntryPoint`` without installing anything.

    ``load`` is a zero-arg callable invoked exactly like the real ``ep.load()`` — pass a function that
    raises to simulate a broken package, or one that returns something non-callable to simulate a
    misdeclared entry point.
    """

    def __init__(self, name: str, load, *, dist: str | None = "fake-dist") -> None:
        self.name = name
        self._load = load
        self.dist = _FakeDist(dist) if dist is not None else None

    def load(self):
        return self._load()


def _theme_loader():
    """A stand-in Theme payload loader — #103 only cares that this is callable, never what it returns."""
    return {"tokens": {}}


# --- tagged installed ----------------------------------------------------------------------------------

def test_discovered_theme_is_tagged_installed():
    ep = _FakeEntryPoint("acme_corp", lambda: _theme_loader, dist="acme-report-theme")
    result = discover_installed_themes(entry_points=[ep], bundled_names=())

    assert set(result.themes) == {"acme_corp"}
    descriptor = result.themes["acme_corp"]
    assert descriptor.provenance == PROVENANCE_INSTALLED
    assert descriptor.distribution == "acme-report-theme"
    assert descriptor.loader is _theme_loader
    assert descriptor.loader() == {"tokens": {}}
    assert result.errors == ()
    assert result.collisions == ()


# --- broken entry points don't kill the rest ------------------------------------------------------------

def test_broken_entry_point_is_collected_as_error_and_does_not_kill_the_rest():
    def _raises():
        raise ImportError("no module named acme_theme")

    broken = _FakeEntryPoint("broken_co", _raises, dist="broken-co-theme")
    good = _FakeEntryPoint("good_co", lambda: _theme_loader, dist="good-co-theme")

    result = discover_installed_themes(entry_points=[broken, good], bundled_names=())

    assert set(result.themes) == {"good_co"}
    assert "broken_co" not in result.themes
    assert len(result.errors) == 1
    assert result.errors[0].entry_point_name == "broken_co"
    assert result.errors[0].distribution == "broken-co-theme"
    assert "ImportError" in result.errors[0].error
    assert "no module named acme_theme" in result.errors[0].error


def test_planted_positive_control_error_is_actually_captured_not_just_absent():
    """A weaker test ('broken_co' not in themes) would still pass even if the implementation swallowed
    the exception with a bare `except Exception: continue` and never appended to `errors`. This test
    fails in exactly that regression, because it demands the specific record, not just the absence."""
    def _raises():
        raise RuntimeError("kaboom")

    result = discover_installed_themes(
        entry_points=[_FakeEntryPoint("planted", _raises)], bundled_names=()
    )
    assert result.themes == {}
    assert len(result.errors) == 1, "error-collection path did not run — exception was swallowed"
    err = result.errors[0]
    assert isinstance(err, ThemeLoadError)
    assert err.entry_point_name == "planted"
    assert err.error == "RuntimeError: kaboom"


def test_non_callable_target_is_collected_as_error():
    """A misdeclared entry point (points at data, not a loader) is a discovery-shaped bug, not a Theme."""
    result = discover_installed_themes(
        entry_points=[_FakeEntryPoint("data_not_code", lambda: {"not": "callable"})],
        bundled_names=(),
    )
    assert result.themes == {}
    assert len(result.errors) == 1
    assert result.errors[0].entry_point_name == "data_not_code"
    assert "did not resolve to a callable loader" in result.errors[0].error


def test_unnamed_entry_point_is_collected_as_error():
    ep = _FakeEntryPoint("", lambda: _theme_loader)
    result = discover_installed_themes(entry_points=[ep], bundled_names=())
    assert result.themes == {}
    assert len(result.errors) == 1
    assert result.errors[0].entry_point_name == "<unnamed>"


# --- collision policy: bundled wins, surfaced not silent -------------------------------------------------

def test_collision_with_bundled_name_bundled_wins_and_is_reported():
    ep = _FakeEntryPoint("default", lambda: _theme_loader, dist="sneaky-theme-pack")
    result = discover_installed_themes(entry_points=[ep], bundled_names=("default", "auto"))

    assert result.themes == {}, "bundled must win — the installed Theme must not shadow it"
    assert result.collisions == (ThemeCollision(name="default", distribution="sneaky-theme-pack"),)
    assert result.errors == ()


def test_collision_uses_real_bundled_registry_by_default():
    """With no explicit `bundled_names`, the real `themes.THEMES` registry is consulted — an installed
    package cannot claim `auto`/`light`/`dark` out from under the shipped Themes."""
    ep = _FakeEntryPoint("dark", lambda: _theme_loader)
    result = discover_installed_themes(entry_points=[ep])
    assert result.themes == {}
    assert result.collisions == (ThemeCollision(name="dark", distribution="fake-dist"),)


def test_collision_between_two_installed_entries_first_wins():
    first = _FakeEntryPoint("corp", lambda: _theme_loader, dist="corp-theme-v1")
    second = _FakeEntryPoint("corp", lambda: _theme_loader, dist="corp-theme-v2")

    result = discover_installed_themes(entry_points=[first, second], bundled_names=())

    assert set(result.themes) == {"corp"}
    assert result.themes["corp"].distribution == "corp-theme-v1"
    assert result.collisions == (ThemeCollision(name="corp", distribution="corp-theme-v2"),)


# --- empty environment -----------------------------------------------------------------------------------

def test_empty_environment_yields_empty_mapping_not_an_error():
    result = discover_installed_themes(entry_points=[], bundled_names=())
    assert result.themes == {}
    assert result.errors == ()
    assert result.collisions == ()


def test_corrupted_metadata_cache_degrades_to_empty_rather_than_raising(monkeypatch):
    """`_installed_entry_points` is the only path that touches real `importlib.metadata`; it must not
    let a broken cache propagate up as an exception from discovery."""

    def _boom(*, group):
        raise RuntimeError("metadata cache is corrupted")

    monkeypatch.setattr(importlib.metadata, "entry_points", _boom)
    result = discover_installed_themes()  # entry_points=None -> hits the real default path
    assert result.themes == {}
    assert result.errors == ()


# --- wired to the right group, by default -----------------------------------------------------------------

def test_default_path_queries_the_declared_entry_point_group(monkeypatch):
    seen_groups = []

    def _fake_entry_points(*, group):
        seen_groups.append(group)
        return [_FakeEntryPoint("watched", lambda: _theme_loader)]

    monkeypatch.setattr(importlib.metadata, "entry_points", _fake_entry_points)
    result = discover_installed_themes(bundled_names=())

    assert seen_groups == [REPORT_THEME_ENTRY_POINT_GROUP]
    assert REPORT_THEME_ENTRY_POINT_GROUP == "scribble.report_themes"
    assert set(result.themes) == {"watched"}


def test_module_internal_seam_is_also_monkeypatchable(monkeypatch):
    """The `_installed_entry_points` seam itself (the same style lotek's own extension discovery uses)
    is an equally valid injection point, for a caller that prefers patching over passing `entry_points=`."""
    fake_eps = [_FakeEntryPoint("patched", lambda: _theme_loader)]
    monkeypatch.setattr(theme_discovery, "_installed_entry_points", lambda: fake_eps)
    result = discover_installed_themes(bundled_names=())
    assert set(result.themes) == {"patched"}
