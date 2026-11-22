"""CLI for managing user-installed Scribble report themes.

A user theme is a ``.toml`` file (and optional ``.docx`` template) placed in the
user themes directory (``~/.local/share/scribble/themes/`` by default, or
``$SCRIBBLE_USER_THEMES_DIR``). Themes registered here are resolved by
``theme_registry.resolve_theme`` between pip-installed and override themes.

Usage (after ``pip install scribble``)::

    scribble-theme add acme path/to/acme.toml [path/to/acme.docx]
    scribble-theme list
    scribble-theme remove acme
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

from scribble.reporting.theme_discovery import (
    _user_themes_dir,
    discover_installed_themes,
    discover_user_themes,
)
from scribble.reporting.theme_registry import list_all_themes


def _themes_dir() -> Path:
    return _user_themes_dir()


def add(name: str, toml_path: Path, docx_path: Path | None = None) -> None:
    """Copy toml (and optional docx) into the user themes directory."""
    name = name.strip().lower()
    if not name or "/" in name or "\\" in name:
        raise ValueError(f"invalid theme name: {name!r}")
    d = _themes_dir()
    d.mkdir(parents=True, exist_ok=True)
    dest_toml = d / f"{name}.toml"
    shutil.copy2(toml_path, dest_toml)
    if docx_path is not None:
        shutil.copy2(docx_path, d / f"{name}.docx")
    print(f"installed theme {name!r} → {dest_toml}")


def remove(name: str) -> None:
    """Remove a user theme by name."""
    name = name.strip().lower()
    d = _themes_dir()
    removed = False
    for ext in (".toml", ".docx"):
        p = d / f"{name}{ext}"
        if p.exists():
            p.unlink()
            removed = True
    if not removed:
        print(f"no user theme named {name!r}", file=sys.stderr)
        sys.exit(1)
    print(f"removed theme {name!r}")


def list_themes() -> None:
    """Print all themes: bundled, pip-installed, and user-managed."""
    pip_themes = discover_installed_themes().themes
    user_themes = discover_user_themes()
    all_themes = list_all_themes()
    for t in all_themes:
        n = t.name
        if n in pip_themes:
            tag = "installed"
        elif n in user_themes:
            tag = "user"
        else:
            tag = "bundled"
        label = t.label or n
        print(f"  {n:<20} {tag:<12} {label}")


def main(argv: list[str] | None = None) -> None:
    import argparse

    ap = argparse.ArgumentParser(prog="scribble-theme", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_add = sub.add_parser("add", help="install a theme from files")
    p_add.add_argument("name", help="theme slug (must match [identity].name in the TOML)")
    p_add.add_argument("toml", type=Path, help="path to the theme .toml file")
    p_add.add_argument("docx", nargs="?", type=Path, default=None,
                       help="path to the theme .docx template (optional)")

    p_rm = sub.add_parser("remove", help="remove a user-installed theme")
    p_rm.add_argument("name")

    sub.add_parser("list", help="list all available themes")

    args = ap.parse_args(argv)
    if args.cmd == "add":
        add(args.name, args.toml, args.docx)
    elif args.cmd == "remove":
        remove(args.name)
    elif args.cmd == "list":
        list_themes()


if __name__ == "__main__":
    main()
