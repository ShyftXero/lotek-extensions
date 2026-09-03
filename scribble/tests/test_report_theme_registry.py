"""Resolving a Theme name across all three Provenances (`reporting/theme_registry.py`).

This module is the piece whose absence got the first cut of Theming rejected: discovery could SEE an
installed Theme, but `get_theme` consulted a frozen three-entry dict, so the Theme was unreachable and
commit 75159ed cut discovery as dead code. What these tests pin:

- a bundled name still resolves, with its payload and `bundled` Provenance,
- an INSTALLED Theme resolves through its entry point, and its payload goes through the same parser,
- an OVERRIDE Theme resolves through the injected lookup,
- precedence is bundled > installed > override even if the sets are not disjoint,
- Provenance travels with the resolution, because the Mark trust gate keys off it,
- every failure degrades to `auto` AND logs, rather than raising or silently vanishing.
"""

from __future__ import annotations

import pytest

from scribble.reporting import theme_registry
from scribble.reporting.theme_discovery import (
    PROVENANCE_INSTALLED,
    InstalledThemeDescriptor,
    ThemeDiscovery,
)
from scribble.reporting.theme_registry import (
    PROVENANCE_BUNDLED,
    PROVENANCE_OVERRIDE,
    list_all_themes,
    resolve_theme,
)

_BRAND_TOML = """
[identity]
name = "brandy"
label = "Brandy Corp"
stamp = "light"

[tokens]
accent = "#123456"
bg = "#ffffff"
"""


def _descriptor(name: str = "brandy", toml: str = _BRAND_TOML) -> InstalledThemeDescriptor:
    return InstalledThemeDescriptor(
        name=name,
        provenance=PROVENANCE_INSTALLED,
        load_toml=lambda: toml,
        distribution="lotek-theme-brandy",
    )


@pytest.fixture
def installed(monkeypatch):
    """Install a fake discovered Theme. Patches the name AS BOUND IN theme_registry, since that module
    does `from ...theme_discovery import discover_installed_themes` and the source-module attribute is
    therefore not what it calls."""

    def _install(*descriptors):
        mapping = {d.name: d for d in descriptors}
        monkeypatch.setattr(
            theme_registry, "discover_installed_themes", lambda: ThemeDiscovery(themes=mapping)
        )

    return _install


# --- bundled -------------------------------------------------------------------------------------------

@pytest.mark.parametrize("name", ["light", "dark"])
def test_a_bundled_theme_resolves_with_its_payload(name):
    resolved = resolve_theme(name)
    assert resolved.theme.name == name
    assert resolved.provenance == PROVENANCE_BUNDLED
    assert resolved.carries_payload
    assert resolved.file is not None
    assert resolved.file.tokens  # the real palette came along


def test_auto_resolves_with_no_payload():
    """`auto` IS the base stylesheet, so having no payload is correct rather than a failure."""
    resolved = resolve_theme("auto")
    assert resolved.theme.name == "auto"
    assert resolved.file is None
    assert not resolved.carries_payload


@pytest.mark.parametrize("given", [None, "", "   ", "NoSuchTheme"])
def test_an_unknown_or_blank_name_degrades_to_auto(given):
    """`?theme=` is untrusted and a client deliverable must render regardless."""
    resolved = resolve_theme(given)
    assert resolved.theme.name == "auto"
    assert resolved.file is None


# --- installed -----------------------------------------------------------------------------------------

def test_an_installed_theme_resolves_through_its_entry_point(installed):
    """The whole point of this module. Before it existed this returned `auto`."""
    installed(_descriptor())
    resolved = resolve_theme("brandy")
    assert resolved.theme.name == "brandy"
    assert resolved.theme.label == "Brandy Corp"
    assert resolved.provenance == "installed"
    assert resolved.file is not None
    assert resolved.file.tokens["accent"] == "#123456"


def test_an_installed_theme_carries_its_own_stamp(installed):
    """A light-tuned brand must be able to say so: with no stamp it would inherit the viewer's dark
    palette underneath its own white-chosen colours. A bundled Theme gets its stamp from the registry;
    an installed one has no registry entry, so it comes from `[identity].stamp`."""
    installed(_descriptor())
    assert resolve_theme("brandy").theme.stamp == "light"
    assert resolve_theme("brandy").theme.html_attr == ' data-theme="light"'


def test_an_installed_theme_is_matched_case_insensitively(installed):
    installed(_descriptor())
    assert resolve_theme("BRANDY").theme.name == "brandy"
    assert resolve_theme("  Brandy  ").theme.name == "brandy"


def test_an_installed_payload_goes_through_the_same_closed_allowlist(installed, caplog):
    """An installed Theme's CODE got to run -- importing it is how the entry point resolved -- but its
    CONTENT earns no extra trust. A token outside the allowlist rejects the whole Theme, exactly as it
    would for a bundled file or an operator upload."""
    hostile = _BRAND_TOML.replace('accent = "#123456"', 'accent = "url(https://evil.example/x)"')
    installed(_descriptor(toml=hostile))
    with caplog.at_level("WARNING"):
        resolved = resolve_theme("brandy")
    assert resolved.theme.name == "auto"  # refused, degraded
    assert any("brandy" in r.getMessage() for r in caplog.records)


def test_an_installed_loader_that_raises_degrades_loudly(installed, caplog):
    def boom() -> str:
        raise RuntimeError("disk gone")

    installed(
        InstalledThemeDescriptor(
            name="brandy", provenance=PROVENANCE_INSTALLED, load_toml=boom, distribution=None
        )
    )
    with caplog.at_level("WARNING"):
        assert resolve_theme("brandy").theme.name == "auto"
    assert any("failed to load" in r.getMessage() for r in caplog.records)


def test_an_installed_loader_returning_a_non_string_degrades_loudly(installed, caplog):
    installed(
        InstalledThemeDescriptor(
            name="brandy",
            provenance=PROVENANCE_INSTALLED,
            load_toml=lambda: {"not": "text"},  # type: ignore[return-value]
            distribution=None,
        )
    )
    with caplog.at_level("WARNING"):
        assert resolve_theme("brandy").theme.name == "auto"
    assert any("instead of TOML text" in r.getMessage() for r in caplog.records)


# --- override ------------------------------------------------------------------------------------------

def test_an_override_theme_resolves_through_the_injected_lookup():
    """`reporting/` holds no DB session, so an operator-supplied Theme arrives as a callable -- the same
    shape `artifact_bytes` already uses for evidence."""
    resolved = resolve_theme("brandy", override_lookup=lambda name: _BRAND_TOML)
    assert resolved.theme.name == "brandy"
    assert resolved.provenance == PROVENANCE_OVERRIDE


def test_no_override_lookup_means_no_override_themes():
    """A caller with no database omits it and gets bundled + installed only, rather than erroring."""
    assert resolve_theme("brandy").theme.name == "auto"


def test_an_override_lookup_returning_none_degrades():
    assert resolve_theme("nope", override_lookup=lambda name: None).theme.name == "auto"


def test_a_malformed_override_degrades_loudly(caplog):
    with caplog.at_level("WARNING"):
        resolved = resolve_theme("brandy", override_lookup=lambda name: "this is not toml {{{")
    assert resolved.theme.name == "auto"
    assert any("brandy" in r.getMessage() for r in caplog.records)


# --- precedence ----------------------------------------------------------------------------------------

def test_bundled_beats_installed(installed):
    """Discovery already excludes a bundled-colliding name, and the upload route refuses one -- so this
    is belt and braces. It is implemented anyway because "the sets are disjoint" is a property two OTHER
    modules maintain, and a registry that resolved differently if either slipped would re-skin a client
    deliverable without saying so."""
    installed(_descriptor(name="light", toml=_BRAND_TOML.replace('"brandy"', '"light"')))
    resolved = resolve_theme("light")
    assert resolved.provenance == PROVENANCE_BUNDLED
    assert resolved.theme.label == "Light"  # the bundled one, not "Brandy Corp"


def test_installed_beats_override(installed):
    """Least-trusted last: an operator pasting data into a form cannot displace a Theme that arrived as
    an installed package."""
    installed(_descriptor())
    resolved = resolve_theme("brandy", override_lookup=lambda name: _BRAND_TOML)
    assert resolved.provenance == "installed"


def test_bundled_beats_override():
    resolved = resolve_theme("light", override_lookup=lambda name: _BRAND_TOML)
    assert resolved.provenance == PROVENANCE_BUNDLED


# --- the switcher list ---------------------------------------------------------------------------------

def test_list_all_themes_includes_every_provenance(installed):
    installed(_descriptor())
    names = [t.name for t in list_all_themes(override_names=("housebrand",))]
    assert names[:3] == ["auto", "light", "dark"]  # bundled first, in registry order
    assert "brandy" in names
    assert "housebrand" in names


def test_list_all_themes_does_not_parse_any_payload(installed):
    """It renders on every page view of every report, so it must stay cheap -- and a Theme broken enough
    to fail parsing should still APPEAR in the switcher and degrade visibly when picked, rather than
    vanishing from the list with no explanation."""
    calls: list[str] = []

    def counting_loader() -> str:
        calls.append("x")
        return _BRAND_TOML

    installed(
        InstalledThemeDescriptor(
            name="brandy",
            provenance=PROVENANCE_INSTALLED,
            load_toml=counting_loader,
            distribution=None,
        )
    )
    assert "brandy" in [t.name for t in list_all_themes()]
    assert calls == []


def test_list_all_themes_never_duplicates_a_name(installed):
    installed(_descriptor(name="light"))
    names = [t.name for t in list_all_themes(override_names=("light", "dark", ""))]
    assert len(names) == len(set(names))
