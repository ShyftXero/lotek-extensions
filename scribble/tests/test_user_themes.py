"""User-managed report themes (ext#106 docx-theme PR): the ``SCRIBBLE_USER_THEMES_DIR`` surface.

Plague found the new user-theme surface shipped with zero tests. This closes that: discovery from the
user dir, resolution precedence (bundled > installed > user > override), ``list_all_themes`` inclusion,
the ``docx_template_path`` wiring, and the ``scribble-theme`` CLI (add/list/remove).

Hermetic: every test points ``SCRIBBLE_USER_THEMES_DIR`` at a ``tmp_path`` so nothing touches the real
``~/.local/share/scribble/themes``. ``discover_user_themes`` is not cached, so no cache clearing is needed.
"""

from __future__ import annotations

import pytest

from scribble.reporting import theme_management
from scribble.reporting.theme_discovery import PROVENANCE_USER, discover_user_themes
from scribble.reporting.theme_registry import list_all_themes, resolve_theme

_ACME_TOML = """
[identity]
name = "acme"
label = "Acme"
stamp = "light"

[tokens]
accent = "#ff0000"
"""


def _write_theme(themes_dir, name: str, *, with_docx: bool = False):
    themes_dir.mkdir(parents=True, exist_ok=True)
    (themes_dir / f"{name}.toml").write_text(_ACME_TOML.replace("acme", name), encoding="utf-8")
    if with_docx:
        (themes_dir / f"{name}.docx").write_bytes(b"PK\x03\x04 fake docx")


@pytest.fixture
def user_themes_dir(tmp_path, monkeypatch):
    d = tmp_path / "user-themes"
    monkeypatch.setenv("SCRIBBLE_USER_THEMES_DIR", str(d))
    return d


def test_discover_user_themes_reads_the_dir(user_themes_dir):
    assert discover_user_themes() == {}  # empty dir (not yet created) → empty, not error
    _write_theme(user_themes_dir, "acme")
    found = discover_user_themes()
    assert set(found) == {"acme"}
    assert found["acme"].provenance == PROVENANCE_USER


def test_resolve_theme_uses_user_theme(user_themes_dir):
    _write_theme(user_themes_dir, "acme")
    resolved = resolve_theme("acme")
    assert resolved.theme.name == "acme"
    assert resolved.provenance == PROVENANCE_USER


def test_user_theme_cannot_shadow_a_bundled_name(user_themes_dir):
    """Bundled wins: a user file named after a bundled theme is excluded from discovery and the
    bundled theme still resolves."""
    _write_theme(user_themes_dir, "light")
    assert "light" not in discover_user_themes()
    assert resolve_theme("light").provenance == "bundled"


def test_user_theme_beats_override(user_themes_dir):
    """Precedence rung: user themes resolve BEFORE the override lookup (step 2b vs step 3)."""
    _write_theme(user_themes_dir, "acme")
    resolved = resolve_theme("acme", override_lookup=lambda _n: _ACME_TOML)
    assert resolved.provenance == PROVENANCE_USER  # user wins, not override


def test_list_all_themes_includes_user_themes(user_themes_dir):
    _write_theme(user_themes_dir, "acme")
    names = {t.name for t in list_all_themes()}
    assert "acme" in names


def test_user_theme_docx_template_path_is_wired(user_themes_dir):
    _write_theme(user_themes_dir, "acme", with_docx=True)
    path = resolve_theme("acme").docx_template_path()
    assert path is not None and path.name == "acme.docx" and path.exists()


def test_cli_add_list_remove(user_themes_dir, tmp_path, capsys):
    src = tmp_path / "src.toml"
    src.write_text(_ACME_TOML, encoding="utf-8")

    theme_management.add("acme", src)
    assert (user_themes_dir / "acme.toml").exists()
    assert "acme" in discover_user_themes()

    theme_management.list_themes()
    out = capsys.readouterr().out
    assert "acme" in out and "user" in out

    theme_management.remove("acme")
    assert not (user_themes_dir / "acme.toml").exists()
    assert discover_user_themes() == {}


def test_cli_remove_unknown_exits_nonzero(user_themes_dir):
    with pytest.raises(SystemExit) as exc:
        theme_management.remove("nope")
    assert exc.value.code == 1
