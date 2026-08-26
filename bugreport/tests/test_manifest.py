"""Drift guard on ``lotek-extension.toml`` — the file that decides whether this extension mounts AT ALL.

Discovery in the host swallows every exception by design, so a typo here does not raise: the extension is
just silently absent. These assertions mirror the host's real rules (``app/extensions.py`` and
``app/extension_schema.py``) closely enough to catch that class of mistake here, where it is cheap.

They are NOT a substitute for a mounted test in lotek — a stub host proves logic, never the mount.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import bugreport
from bugreport.models import Base

MANIFEST = Path(__file__).resolve().parent.parent / "lotek-extension.toml"
DATA = tomllib.loads(MANIFEST.read_text())


def test_the_mount_table_matches_the_package():
    mount = DATA["mount"]
    assert mount["name"] == "bugreport"
    assert mount["entrypoint"] == "bugreport"
    assert mount["url_prefix"] == "/bugreport"
    assert callable(bugreport.register), "the entrypoint module must expose register(...)"


def test_the_machine_prefix_is_a_strict_subpath_of_the_url_prefix():
    """The host EXEMPTS this absolute prefix from the session gate and CSRF, so a prefix that escaped
    ``url_prefix`` would disable those gates on paths this extension does not own."""
    base = DATA["mount"]["url_prefix"].rstrip("/")
    raw = DATA["host"]["machine_prefix"]
    assert ".." not in raw and not any(c.isspace() for c in raw)
    resolved = (base + (raw if raw.startswith("/") else "/" + raw)).rstrip("/") + "/"
    assert resolved.startswith(base + "/") and len(resolved) > len(base) + 1


def test_the_db_table_declares_this_packages_base_and_prefix():
    db = DATA["db"]
    assert db["base"] == "bugreport.models:Base"
    assert db["base"].split(":")[0].startswith("bugreport"), "a Base may only be declared self-scoped"
    prefix = db["table_prefix"]
    assert re.fullmatch(r"[a-z][a-z0-9_]{1,30}_", prefix), "the host's _PREFIX_RE rejects anything else"
    owned = {t.name for t in Base.metadata.sorted_tables}
    assert owned, "no tables registered — did models.py get imported?"
    assert all(name.startswith(prefix) for name in owned), owned


def test_every_declared_doc_and_manifest_path_exists_to_be_force_included():
    """These are copied into the wheel at BUILD time by ``[tool.hatch...force-include]``. A path that
    does not exist here ships a wheel with no manifest, and discovery silently skips the extension."""
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
    declared = {f"ext:bugreport:{v}" for v in DATA["audit"]["verbs"]}
    src = (MANIFEST.parent / "bugreport").rglob("*.py")
    emitted = set()
    for path in src:
        emitted |= set(re.findall(r'"(ext:bugreport:[a-z_]+)"', path.read_text()))
    assert emitted, "no audit action literals found — did the audit call site move?"
    assert emitted == declared, f"emitted={emitted} declared={declared}"
