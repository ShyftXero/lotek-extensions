"""Discovery of installed report Themes (#103) — the ``scribble.report_themes`` entry point.

Covers ``scribble.reporting.theme_discovery``:

- a discovered Theme is tagged ``provenance == PROVENANCE_INSTALLED`` and its ``load_toml`` returns the
  TOML text a stand-in entry point supplied, unchanged,
- a broken entry point (raising loader, non-callable target, unnamed entry, a ``.name`` property that
  itself raises) is collected as an error and does not stop the rest of discovery,
- a PLANTED positive control proving the error is actually CAPTURED, not merely "the theme didn't
  appear" (a swallow-the-exception regression would still pass a weaker "not in themes" assertion),
- a callable that returns something other than a ``str`` is refused with a clear, immediate error when
  ``load_toml()`` is actually called — never a crash three calls later inside the TOML parser,
- collision with a bundled name: bundled wins, excluded from ``themes``, and reported in ``collisions``,
  including when only the CASE differs ("Dark" vs bundled "dark"),
- collision between two installed entries: first wins, the rest collide,
- an empty environment yields an empty (not erroring) result,
- the real ``importlib.metadata.entry_points`` default path is wired to the right group name, and its
  result is cached across calls until explicitly cleared — while a call passing either seam explicitly
  bypasses the cache entirely.

Fakes entry points by hand (objects carrying ``.name``, ``.load()``, ``.dist``) — no package is ever
actually installed.
"""

from __future__ import annotations

import importlib.metadata

import pytest

from scribble.reporting import theme_discovery
from scribble.reporting.theme_discovery import (
    PROVENANCE_INSTALLED,
    REPORT_THEME_ENTRY_POINT_GROUP,
    ThemeCollision,
    ThemeLoadError,
    discover_installed_themes,
)


@pytest.fixture(autouse=True)
def _isolated_theme_discovery_cache():
    """The module-level cache (see ``theme_discovery``'s "Caching" docstring section) only engages for
    the fully-default call shape (``entry_points=None``, ``bundled_names=None``) — several tests below
    exercise exactly that shape against a monkeypatched ``importlib.metadata.entry_points``. Without
    clearing around every test, whichever of those tests happens to run FIRST would freeze its result
    into every later one, regardless of what that later test monkeypatches. Clearing both before and
    after makes this file's outcome independent of execution order.
    """
    theme_discovery.clear_theme_discovery_cache()
    yield
    theme_discovery.clear_theme_discovery_cache()


class _FakeDist:
    def __init__(self, name: str) -> None:
        self.name = name


class _FakeEntryPoint:
    """Stands in for ``importlib.metadata.EntryPoint`` without installing anything.

    ``load`` is a zero-arg callable invoked exactly like the real ``ep.load()`` — pass a function that
    raises to simulate a broken package, or one that returns something non-callable (or a callable
    whose OWN return value is not a ``str``) to simulate a misdeclared entry point.
    """

    def __init__(self, name: str, load, *, dist: str | None = "fake-dist") -> None:
        self.name = name
        self._load = load
        self.dist = _FakeDist(dist) if dist is not None else None

    def load(self):
        return self._load()


class _RaisingNameEntryPoint:
    """An entry point whose ``.name`` ATTRIBUTE ACCESS itself raises — distinct from an entry point that
    merely declares an empty name. Simulates a genuinely malformed ``importlib.metadata`` object, which
    ``discover_installed_themes`` must survive exactly like every other malformed shape (see ticket rule
    5: "whatever an entry point does").
    """

    def __init__(self, load, *, dist: str | None = "fake-dist") -> None:
        self._load = load
        self.dist = _FakeDist(dist) if dist is not None else None

    @property
    def name(self):
        raise RuntimeError("this distribution's metadata is corrupt")

    def load(self):
        return self._load()


def _toml_loader() -> str:
    """A stand-in installed-Theme payload: the callable an entry point resolves to, per the pinned
    contract — zero arguments, returns the Theme's TOML text as a plain ``str``."""
    return '[identity]\nname = "acme_corp"\nlabel = "Acme Corp"\n'


# --- tagged installed, payload is TOML text ---------------------------------------------------------

def test_discovered_theme_is_tagged_installed_and_load_toml_returns_text():
    ep = _FakeEntryPoint("acme_corp", lambda: _toml_loader, dist="acme-report-theme")
    result = discover_installed_themes(entry_points=[ep], bundled_names=())

    assert set(result.themes) == {"acme_corp"}
    descriptor = result.themes["acme_corp"]
    assert descriptor.name == "acme_corp"
    assert descriptor.provenance == PROVENANCE_INSTALLED
    assert descriptor.distribution == "acme-report-theme"
    text = descriptor.load_toml()
    assert isinstance(text, str)
    assert text == _toml_loader()
    assert result.errors == ()
    assert result.collisions == ()


# --- broken entry points don't kill the rest ------------------------------------------------------------

def test_broken_entry_point_is_collected_as_error_and_does_not_kill_the_rest():
    def _raises():
        raise ImportError("no module named acme_theme")

    broken = _FakeEntryPoint("broken_co", _raises, dist="broken-co-theme")
    good = _FakeEntryPoint("good_co", lambda: _toml_loader, dist="good-co-theme")

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
    assert "did not resolve to a callable" in result.errors[0].error


def test_unnamed_entry_point_is_collected_as_error():
    ep = _FakeEntryPoint("", lambda: _toml_loader)
    result = discover_installed_themes(entry_points=[ep], bundled_names=())
    assert result.themes == {}
    assert len(result.errors) == 1
    assert result.errors[0].entry_point_name == "<unnamed>"


def test_a_name_property_that_raises_is_collected_as_error_not_a_crash():
    """Rule 5 of the ticket, verbatim: discovery "must never raise, whatever an entry point does —
    including a `.name` property that raises". `getattr(ep, "name", ...)` alone would NOT save this
    case (a raising property is not an AttributeError-on-missing-attribute), so this exercises the
    broader `try/except` around the name read, not just the empty-string path."""
    ep = _RaisingNameEntryPoint(lambda: _toml_loader)
    result = discover_installed_themes(entry_points=[ep], bundled_names=())
    assert result.themes == {}
    assert len(result.errors) == 1
    assert result.errors[0].entry_point_name == "<unnamed>"


class _RaisingDistEntryPoint:
    """An entry point whose ``.dist`` PROPERTY raises on access.

    Distinct from ``_RaisingNameEntryPoint`` and not redundant with it: on a real
    ``importlib.metadata.EntryPoint``, ``.dist`` IS a property, so a corrupted metadata cache can raise
    from it. ``getattr(ep, "dist", None)`` does not help — a default only substitutes on
    AttributeError. The value is used solely for a human-readable label in an error record, which makes
    crashing over it especially poor value.
    """

    def __init__(self, load) -> None:
        self.name = "brandy"
        self._load = load

    @property
    def dist(self):
        raise RuntimeError("corrupted distribution metadata")

    def load(self):
        return self._load


def test_a_raising_dist_property_does_not_break_discovery():
    """Regression, #103 adversarial review: this propagated an uncaught RuntimeError, which would take
    down discovery of EVERY Theme — bundled included — for every render in the process, over a label
    used only inside an error message. `.name` was already defended; `.dist` was missed by omission."""
    ep = _RaisingDistEntryPoint(lambda: _toml_loader)
    result = discover_installed_themes(entry_points=[ep], bundled_names=())
    assert "brandy" in result.themes
    assert result.themes["brandy"].distribution is None  # degraded to "unknown", not crashed
    assert result.errors == ()


def test_a_raising_dist_on_an_otherwise_broken_entry_point_still_collects_the_error():
    """Both failures at once: the label is unavailable AND the target is unloadable. The error must
    still be collected rather than either crashing or being dropped."""

    def boom():
        raise ImportError("no module named brandy")

    ep = _RaisingDistEntryPoint(boom)
    ep.load = boom  # type: ignore[method-assign]
    result = discover_installed_themes(entry_points=[ep], bundled_names=())
    assert result.themes == {}
    assert len(result.errors) == 1
    assert result.errors[0].distribution is None


def test_the_discovered_themes_mapping_is_read_only():
    """The cache hands every caller in the process the SAME result object, so an in-place merge -- the
    obvious way to write "bundled wins, then add installed" -- would corrupt the registry for every
    later request rather than just its own. Provenance drives the Mark trust gate, so a corrupted
    mapping is a route to a Theme carrying the wrong trust level. Merge by building a new dict."""
    ep = _FakeEntryPoint("brandy", lambda: _toml_loader)
    result = discover_installed_themes(entry_points=[ep], bundled_names=())
    with pytest.raises(TypeError):
        result.themes["injected"] = result.themes["brandy"]  # type: ignore[index]
    with pytest.raises(AttributeError):
        result.themes.update({})  # type: ignore[attr-defined]
    assert list(result.themes) == ["brandy"]


# --- the str payload contract -----------------------------------------------------------------------

def test_a_callable_returning_a_non_str_is_an_error_not_a_crash_when_load_toml_is_called():
    """Discovery still succeeds — the entry point resolved to something callable — but the WRAPPED
    `load_toml` refuses the bad value immediately and by name by the time a render-time caller invokes
    it, rather than letting a dict/None/bytes silently reach `tomllib.loads` several calls away from
    where the actual mistake was made."""

    def _returns_a_dict():
        return {"tokens": {"accent": "#123456"}}

    ep = _FakeEntryPoint("data_shaped_theme", lambda: _returns_a_dict, dist="data-shaped-theme")
    result = discover_installed_themes(entry_points=[ep], bundled_names=())

    assert set(result.themes) == {"data_shaped_theme"}
    assert result.errors == ()  # discovery itself did not fail — only invoking the payload does
    descriptor = result.themes["data_shaped_theme"]
    with pytest.raises(TypeError, match="data_shaped_theme"):
        descriptor.load_toml()


def test_a_callable_returning_none_is_also_refused_not_a_crash():
    ep = _FakeEntryPoint("returns_none", lambda: (lambda: None))
    result = discover_installed_themes(entry_points=[ep], bundled_names=())
    descriptor = result.themes["returns_none"]
    with pytest.raises(TypeError):
        descriptor.load_toml()


def test_a_well_behaved_load_toml_is_never_invoked_during_discovery():
    """See the module docstring's "Why the callable is never invoked here": discovery must not pay the
    cost of materialising every installed Theme's payload just to list names. A loader that would
    raise if CALLED must still discover cleanly, because discovery never calls it."""
    calls = []

    def _would_raise_if_called():
        calls.append(1)
        raise AssertionError("load_toml must not run during discovery")

    ep = _FakeEntryPoint("never_invoked", lambda: _would_raise_if_called)
    result = discover_installed_themes(entry_points=[ep], bundled_names=())
    assert set(result.themes) == {"never_invoked"}
    assert calls == []


# --- collision policy: bundled wins, surfaced not silent -------------------------------------------------

def test_collision_with_bundled_name_bundled_wins_and_is_reported():
    ep = _FakeEntryPoint("default", lambda: _toml_loader, dist="sneaky-theme-pack")
    result = discover_installed_themes(entry_points=[ep], bundled_names=("default", "auto"))

    assert result.themes == {}, "bundled must win — the installed Theme must not shadow it"
    assert result.collisions == (ThemeCollision(name="default", distribution="sneaky-theme-pack"),)
    assert result.errors == ()


def test_case_differing_collision_with_bundled_name_is_also_caught():
    """`themes.get_theme` lower-cases before lookup, so an installed Theme named "Dark" would otherwise
    dodge the collision check and then get selected by `?theme=dark` anyway — precisely the surprise
    re-skin the bundled-wins policy exists to prevent. See the module docstring's case-folding note."""
    ep = _FakeEntryPoint("Dark", lambda: _toml_loader, dist="sneaky-dark-pack")
    result = discover_installed_themes(entry_points=[ep], bundled_names=("auto", "light", "dark"))

    assert result.themes == {}
    assert result.collisions == (ThemeCollision(name="dark", distribution="sneaky-dark-pack"),)


def test_collision_uses_real_bundled_registry_by_default():
    """With no explicit `bundled_names`, the real `themes.THEMES` registry is consulted — an installed
    package cannot claim `auto`/`light`/`dark` out from under the shipped Themes."""
    ep = _FakeEntryPoint("dark", lambda: _toml_loader)
    result = discover_installed_themes(entry_points=[ep])
    assert result.themes == {}
    assert result.collisions == (ThemeCollision(name="dark", distribution="fake-dist"),)


def test_collision_between_two_installed_entries_first_wins():
    first = _FakeEntryPoint("corp", lambda: _toml_loader, dist="corp-theme-v1")
    second = _FakeEntryPoint("corp", lambda: _toml_loader, dist="corp-theme-v2")

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
        return [_FakeEntryPoint("watched", lambda: _toml_loader)]

    monkeypatch.setattr(importlib.metadata, "entry_points", _fake_entry_points)
    result = discover_installed_themes(bundled_names=())

    assert seen_groups == [REPORT_THEME_ENTRY_POINT_GROUP]
    assert REPORT_THEME_ENTRY_POINT_GROUP == "scribble.report_themes"
    assert set(result.themes) == {"watched"}


def test_module_internal_seam_is_also_monkeypatchable(monkeypatch):
    """The `_installed_entry_points` seam itself (the same style lotek's own extension discovery uses)
    is an equally valid injection point, for a caller that prefers patching over passing `entry_points=`."""
    fake_eps = [_FakeEntryPoint("patched", lambda: _toml_loader)]
    monkeypatch.setattr(theme_discovery, "_installed_entry_points", lambda: fake_eps)
    result = discover_installed_themes(bundled_names=())
    assert set(result.themes) == {"patched"}


# --- caching: per-process for the default path, explicit seams always bypass it --------------------------

def test_default_path_result_is_cached_across_repeated_calls(monkeypatch):
    seen_groups = []

    def _fake_entry_points(*, group):
        seen_groups.append(group)
        return [_FakeEntryPoint("cached_co", lambda: _toml_loader)]

    monkeypatch.setattr(importlib.metadata, "entry_points", _fake_entry_points)

    first = discover_installed_themes()
    second = discover_installed_themes()

    assert first is second, "the memoised ThemeDiscovery must be returned, not a freshly built one"
    assert seen_groups == [REPORT_THEME_ENTRY_POINT_GROUP], (
        "a second default-path call must not re-touch importlib.metadata at all"
    )


def test_clear_theme_discovery_cache_forces_a_fresh_pass(monkeypatch):
    monkeypatch.setattr(
        importlib.metadata,
        "entry_points",
        lambda *, group: [_FakeEntryPoint("v1_co", lambda: _toml_loader)],
    )
    first = discover_installed_themes()
    assert set(first.themes) == {"v1_co"}

    theme_discovery.clear_theme_discovery_cache()
    monkeypatch.setattr(
        importlib.metadata,
        "entry_points",
        lambda *, group: [_FakeEntryPoint("v2_co", lambda: _toml_loader)],
    )
    second = discover_installed_themes()

    assert set(second.themes) == {"v2_co"}, "clearing the cache must force a real re-scan"
    assert first is not second


def test_explicit_seams_always_bypass_the_cache():
    """A call that supplies EITHER seam explicitly — as every other test in this module does — must
    never read from or write to the process-wide cache; otherwise two tests exercising different
    synthetic environments back-to-back would leak into each other."""
    first = discover_installed_themes(
        entry_points=[_FakeEntryPoint("first_synthetic", lambda: _toml_loader)], bundled_names=()
    )
    second = discover_installed_themes(
        entry_points=[_FakeEntryPoint("second_synthetic", lambda: _toml_loader)], bundled_names=()
    )
    assert first is not second
    assert set(first.themes) == {"first_synthetic"}
    assert set(second.themes) == {"second_synthetic"}
