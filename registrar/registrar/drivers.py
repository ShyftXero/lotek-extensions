"""Minimal provider-driver abstraction — the third pluggability axis (like fraction/vector are minimal).

A driver is an ABC; concrete backends implement it. The MVP ships one **in-memory** backend per axis
(``null``) so the whole gate/audit/staging machinery is exercised without any egress or credentials;
real backends (DigitalOcean/libcloud compute + DNS, Twilio SMS) implement the same ABCs later.

**Effect-locality tiers (B4/INV-EXT-03), not reversibility.** Any verb whose effect is observable
OUTSIDE this instance — a provider mutation, a DNS record at a resolver, an SMS to a real phone — is
``confirm``-tier and is never agent-autonomous. Read/inventory verbs are ``read``. The host RESOLVES a
verb's tier and fails closed: an unclassified verb is treated as ``confirm`` and not agent-exposed.
"""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from registrar.enums import Tier


@dataclass
class VerbSpec:
    name: str
    tier: Tier


@dataclass
class NodeInfo:
    provider_ref: str
    name: str
    ip: str | None = None
    state: str = "active"


class ComputeDriver(ABC):
    """Provision/track hosts. ``list_nodes``/``get_node`` are reads; create/destroy are outward (confirm)."""

    name = "null"
    VERBS = {
        "list_nodes": Tier.direct, "get_node": Tier.direct,
        "create_node": Tier.confirm, "destroy_node": Tier.confirm,
    }

    @abstractmethod
    def list_nodes(self) -> list[NodeInfo]: ...
    @abstractmethod
    def create_node(self, *, name: str, size: str = "s-1vcpu-1gb", region: str = "nyc1") -> NodeInfo: ...
    @abstractmethod
    def destroy_node(self, provider_ref: str) -> bool: ...


class DnsDriver(ABC):
    """Manage DNS. Note: ``upsert_record`` is **confirm**, not direct — a record write takes effect at
    third-party resolvers and is cached for its TTL, so its effect is outward (B4)."""

    name = "null"
    VERBS = {
        "list_records": Tier.direct,
        "upsert_record": Tier.confirm, "register_domain": Tier.confirm,
    }

    @abstractmethod
    def list_records(self, domain: str) -> list[dict]: ...
    @abstractmethod
    def upsert_record(self, domain: str, *, rtype: str, name: str, value: str, ttl: int = 300) -> dict: ...
    @abstractmethod
    def register_domain(self, name: str) -> dict: ...


class SmsDriver(ABC):
    name = "null"
    VERBS = {"send_sms": Tier.confirm}

    @abstractmethod
    def send_sms(self, *, to: str, body: str) -> dict: ...


# ── the in-memory MVP backends (no egress, no credentials) ──────────────────────────────────────────

@dataclass
class NullCompute(ComputeDriver):
    name: str = "null"
    _nodes: dict[str, NodeInfo] = field(default_factory=dict)

    def list_nodes(self) -> list[NodeInfo]:
        return list(self._nodes.values())

    def create_node(self, *, name: str, size: str = "s-1vcpu-1gb", region: str = "nyc1") -> NodeInfo:
        ref = f"null-{uuid.uuid4().hex[:12]}"
        info = NodeInfo(provider_ref=ref, name=name, ip="203.0.113.10", state="active")
        self._nodes[ref] = info
        return info

    def destroy_node(self, provider_ref: str) -> bool:
        return self._nodes.pop(provider_ref, None) is not None


@dataclass
class NullDns(DnsDriver):
    name: str = "null"
    _records: list[dict] = field(default_factory=list)

    def list_records(self, domain: str) -> list[dict]:
        return [r for r in self._records if r["domain"] == domain]

    def upsert_record(self, domain: str, *, rtype: str, name: str, value: str, ttl: int = 300) -> dict:
        rec = {"domain": domain, "rtype": rtype, "name": name, "value": value, "ttl": ttl}
        self._records.append(rec)
        return rec

    def register_domain(self, name: str) -> dict:
        return {"domain": name, "registered": True}


@dataclass
class NullSms(SmsDriver):
    name: str = "null"
    sent: list[dict] = field(default_factory=list)

    def send_sms(self, *, to: str, body: str) -> dict:
        rec = {"to": to, "body": body, "status": "queued"}
        self.sent.append(rec)
        return rec


_COMPUTE = {"null": NullCompute}
_DNS = {"null": NullDns}
_SMS = {"null": NullSms}


def get_compute(provider: str = "null") -> ComputeDriver:
    return _COMPUTE.get(provider, NullCompute)()


def get_dns(provider: str = "null") -> DnsDriver:
    return _DNS.get(provider, NullDns)()


def get_sms(provider: str = "null") -> SmsDriver:
    return _SMS.get(provider, NullSms)()


# The host RESOLVES a verb's tier and FAILS CLOSED (INV-EXT-03): an unknown verb is confirm-tier.
_ALL_VERBS: dict[str, Tier] = {**ComputeDriver.VERBS, **DnsDriver.VERBS, **SmsDriver.VERBS}


def tier_of(verb: str) -> Tier:
    return _ALL_VERBS.get(verb, Tier.confirm)
