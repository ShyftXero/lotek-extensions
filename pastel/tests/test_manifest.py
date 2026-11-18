"""Drift guard on ``lotek-extension.toml`` — the manifest that decides whether this extension mounts.

Mirrors the host's real discovery rules closely enough to catch typos here, before they silently
prevent mounting. NOT a substitute for a mounted test in lotek.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pastel
from pastel.models import Base

MANIFEST = Path(__file__).resolve().parent.parent / "lotek-extension.toml"
DATA = tomllib.loads(MANIFEST.read_text())


def test_the_mount_table_matches_the_package():
    mount = DATA["mount"]
    assert mount["name"] == "pastel"
    assert mount["entrypoint"] == "pastel"
    assert mount["url_prefix"] == "/pastel"
    assert callable(pastel.register), "the entrypoint module must expose register(...)"


def test_machine_prefix_is_strict_subpath_of_url_prefix():
    """The host EXEMPTS this prefix from the session gate and CSRF; a prefix escaping url_prefix
    would disable those gates on paths this extension does not own."""
    base = DATA["mount"]["url_prefix"].rstrip("/")
    raw = DATA["host"]["machine_prefix"]
    assert ".." not in raw and not any(c.isspace() for c in raw)
    resolved = (base + (raw if raw.startswith("/") else "/" + raw)).rstrip("/") + "/"
    assert resolved.startswith(base + "/") and len(resolved) > len(base) + 1


def test_public_prefix_is_strict_subpath_of_url_prefix():
    """Same constraint: the host skips the session gate for paths under this prefix."""
    base = DATA["mount"]["url_prefix"].rstrip("/")
    raw = DATA["host"]["public_prefix"]
    assert ".." not in raw and not any(c.isspace() for c in raw)
    resolved = (base + (raw if raw.startswith("/") else "/" + raw)).rstrip("/") + "/"
    assert resolved.startswith(base + "/") and len(resolved) > len(base) + 1


def test_db_table_declares_this_packages_base_and_prefix():
    db = DATA["db"]
    assert db["base"] == "pastel.models:Base"
    assert db["base"].split(":")[0].startswith("pastel")
    prefix = db["table_prefix"]
    assert re.fullmatch(r"[a-z][a-z0-9_]{1,30}_", prefix)
    owned = {t.name for t in Base.metadata.sorted_tables}
    assert owned, "no tables registered — did models.py get imported?"
    assert all(name.startswith(prefix) for name in owned), owned


def test_every_declared_doc_and_manifest_path_exists():
    root = MANIFEST.parent
    force = tomllib.loads((root / "pyproject.toml").read_text())
    included = force["tool"]["hatch"]["build"]["targets"]["wheel"]["force-include"]
    assert "lotek-extension.toml" in included
    for src in included:
        assert (root / src).is_file(), f"force-included {src!r} does not exist"
    assert (root / DATA["host"]["docs"]).is_file()


def test_declared_audit_verbs_match_emitted_actions():
    """INV-AUDIT-03: an emitted action not declared here is written to the trail but invisible in the
    /admin/audit filter."""
    declared = {f"ext:pastel:{v}" for v in DATA["audit"]["verbs"]}
    src = (MANIFEST.parent / "pastel").rglob("*.py")
    emitted = set()
    for path in src:
        emitted |= set(re.findall(r'"(ext:pastel:[a-z_]+)"', path.read_text()))
    assert emitted, "no audit action literals found — did the audit call site move?"
    assert emitted == declared, f"emitted={emitted} declared={declared}"


def test_core_ref_columns_are_uuid_typed():
    """INV-INTEGRITY-03. owner_id holds a core (host) id; core v2 keys every PK on UUIDv7.
    An Integer/String column here is the ``cannot cast type uuid to integer`` red path."""
    from sqlalchemy import Uuid

    cols = {c.name: c for c in Base.metadata.tables["pastel_pastes"].columns}
    assert isinstance(cols["id"].type, Uuid), f"id is {cols['id'].type!r}"
    assert isinstance(cols["owner_id"].type, Uuid), f"owner_id is {cols['owner_id'].type!r}"
    core_refs = [n for n in cols if n.endswith(("_id", "_by")) and n != "id"]
    assert core_refs == ["owner_id"], f"new core-ref column(s) {core_refs} — check their type"
