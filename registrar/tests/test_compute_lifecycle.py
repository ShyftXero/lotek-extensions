"""New compute lifecycle verbs and DNS delete — null-backend unit checks (INV-EXT-03)."""

from __future__ import annotations

from registrar.drivers import NullCompute, NullDns, _ALL_VERBS, tier_of
from registrar.enums import Tier


def test_new_verbs_are_confirm_tier():
    outward = ("start_node", "stop_node", "restart_node", "upload_ssh_key", "set_firewall_rules",
                "delete_record")
    for v in outward:
        assert v in _ALL_VERBS, f"verb {v!r} missing from _ALL_VERBS"
        assert tier_of(v) is Tier.confirm, f"{v} must be confirm-tier (outward effect)"


def test_null_compute_lifecycle():
    c = NullCompute()
    node = c.create_node(name="test-vm")
    ref = node.provider_ref

    stopped = c.stop_node(ref)
    assert stopped.state == "off"

    started = c.start_node(ref)
    assert started.state == "active"

    restarted = c.restart_node(ref)
    assert restarted.state == "active"

    key = c.upload_ssh_key(name="operator-key", public_key="ssh-ed25519 AAAA...")
    assert key["name"] == "operator-key"
    assert "id" in key

    fw = c.set_firewall_rules(ref, rules=[{"port": 22, "src": "10.0.0.1/32"}])
    assert fw["rules_applied"] == 1

    assert c.destroy_node(ref)


def test_null_dns_delete_record():
    d = NullDns()
    rec = d.upsert_record("example.com", rtype="A", name="www", value="1.2.3.4")
    record_id = rec["id"]
    assert d.list_records("example.com")
    assert d.delete_record("example.com", record_id)
    assert not d.list_records("example.com")


def test_null_dns_upsert_replaces_existing():
    d = NullDns()
    d.upsert_record("example.com", rtype="A", name="@", value="1.1.1.1")
    d.upsert_record("example.com", rtype="A", name="@", value="2.2.2.2")
    records = d.list_records("example.com")
    assert len(records) == 1
    assert records[0]["value"] == "2.2.2.2"
