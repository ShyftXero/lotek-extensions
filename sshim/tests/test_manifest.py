"""Drift guard on ``lotek-extension.toml`` — the file that decides whether sshim mounts AT ALL.

Discovery in the host swallows every exception by design, so a typo here does not raise: the extension
is just silently absent. These assertions mirror the host's real rules closely enough to catch that
class of mistake here, where it is cheap. Mirror of ``bugreport/tests/test_manifest.py``.
"""
from __future__ import annotations

import re
import tomllib
from pathlib import Path

import sshim
from sshim.models import Base

MANIFEST = Path(__file__).resolve().parent.parent / "lotek-extension.toml"
DATA = tomllib.loads(MANIFEST.read_text())


def test_the_mount_table_matches_the_package():
    mount = DATA["mount"]
    assert mount["name"] == "sshim"
    assert mount["entrypoint"] == "sshim"
    assert mount["url_prefix"] == "/sshim"
    assert callable(sshim.register), "the entrypoint module must expose register(...)"


def test_the_machine_prefix_is_a_strict_subpath_of_the_url_prefix():
    base = DATA["mount"]["url_prefix"].rstrip("/")
    raw = DATA["host"]["machine_prefix"]
    assert ".." not in raw and not any(c.isspace() for c in raw)
    resolved = (base + (raw if raw.startswith("/") else "/" + raw)).rstrip("/") + "/"
    assert resolved.startswith(base + "/") and len(resolved) > len(base) + 1


def test_the_db_table_declares_this_packages_base_and_prefix():
    db = DATA["db"]
    assert db["base"] == "sshim.models:Base"
    assert db["base"].split(":")[0].startswith("sshim"), "a Base may only be declared self-scoped"
    prefix = db["table_prefix"]
    assert re.fullmatch(r"[a-z][a-z0-9_]{1,30}_", prefix), "the host's _PREFIX_RE rejects anything else"
    owned = {t.name for t in Base.metadata.sorted_tables}
    assert owned, "no tables registered — did models.py get imported?"
    assert all(name.startswith(prefix) for name in owned), owned


def test_every_declared_doc_and_manifest_path_exists_to_be_force_included():
    root = MANIFEST.parent
    force = tomllib.loads((root / "pyproject.toml").read_text())
    included = force["tool"]["hatch"]["build"]["targets"]["wheel"]["force-include"]
    assert "lotek-extension.toml" in included
    for src in included:
        assert (root / src).is_file(), f"force-included {src} does not exist"
    assert (root / DATA["host"]["docs"]).is_file()


def test_the_declared_audit_verbs_are_exactly_what_the_code_emits():
    """INV-AUDIT-03's reader half: an emitted action the ``[audit]`` table does not declare is written to
    core's trail but missing from the /admin/audit filter, so nobody can select it."""
    declared = {f"ext:sshim:{v}" for v in DATA["audit"]["verbs"]}
    emitted = set()
    for path in (MANIFEST.parent / "sshim").rglob("*.py"):
        emitted |= set(re.findall(r'"(ext:sshim:[a-z_]+)"', path.read_text()))
    assert emitted, "no audit action literals found — did the audit call site move?"
    assert emitted == declared, f"emitted={emitted} declared={declared}"


def test_the_settings_keys_are_what_deps_reads():
    keys = {s["key"] for s in DATA["settings"]}
    assert keys == {"default_relay_host", "max_ttl_minutes"}


def test_the_core_ref_columns_are_uuid_typed():
    """INV-INTEGRITY-03. ``owner_id`` holds a CORE (host) id, and ``profile_id`` an sshim id — core v2
    keys every surrogate PK on UUIDv7, so an ``Integer``/``String`` here is the `cannot cast type uuid to
    integer` red path. SQLite stores the value silently; assert on the declared type instead."""
    from sqlalchemy import Uuid

    for table in ("sshim_profiles", "sshim_sessions"):
        cols = {c.name: c for c in Base.metadata.tables[table].columns}
        core_refs = [n for n in cols if n.endswith(("_id", "_by"))]
        for name in core_refs:
            assert isinstance(cols[name].type, Uuid), f"{table}.{name} is {cols[name].type!r}, not Uuid"
