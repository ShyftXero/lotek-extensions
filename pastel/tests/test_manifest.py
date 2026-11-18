"""The mount manifest is structurally sound and its declared audit vocabulary matches the code.

INV-AUDIT-03's reader half: a verb a call site emits but the manifest does not declare is written to the
trail but not selectable by the host's audit filter. So the set of `[audit] verbs` must equal the set of
bare actions passed to `host_audit(...)` anywhere in the package.
"""

from __future__ import annotations

import pathlib
import re
import tomllib

ROOT = pathlib.Path(__file__).resolve().parents[1]
PKG = ROOT / "pastel"                       # the package dir (for the call-site scan)
MANIFEST = ROOT / "lotek-extension.toml"    # ships at the project root, not inside the package


def _manifest() -> dict:
    return tomllib.loads(MANIFEST.read_text())


def test_manifest_parses_and_has_required_tables():
    m = _manifest()
    assert m["mount"]["name"] == "pastel"
    assert m["mount"]["entrypoint"] == "pastel"
    assert m["mount"]["url_prefix"] == "/pastel"
    # machine + public surfaces must be strict sub-paths of the mount.
    assert m["host"]["machine_prefix"] == "/machine"
    assert m["host"]["public_prefix"] == "/p"
    assert m["db"]["table_prefix"] == "pastel_"
    assert m["db"]["base"] == "pastel.models:Base"


def test_audit_verbs_match_call_sites():
    declared = set(_manifest()["audit"]["verbs"])
    # Every `host_audit(db, "<verb>", ...)` call across the package, verb as a string literal.
    emitted = set()
    for py in PKG.rglob("*.py"):
        for mo in re.finditer(r'host_audit\(\s*db\s*,\s*"([a-z_]+)"', py.read_text()):
            emitted.add(mo.group(1))
    assert emitted, "expected at least one host_audit call site"
    assert emitted == declared, f"manifest {declared} != call sites {emitted}"


def test_register_is_importable():
    import pastel

    assert callable(pastel.register)
