"""Provider-driver abstraction — pluggable compute, DNS, and SMS backends.

A driver is an ABC; concrete backends implement it. The MVP ships one **in-memory** backend per axis
(``null``) so the whole gate/audit/staging machinery is exercised without any egress or credentials.

Real backends:
- ``LibcloudCompute``: DigitalOcean, Vultr, AWS, Azure via apache-libcloud (``providers`` extra).
- ``IncusCompute``: Incus VMs via the Incus REST API over a Unix socket (no extra dep).
- ``LibcloudDns``: DigitalOcean DNS, GoDaddy, Namecheap via apache-libcloud DNS module.
- ``NameComDns``: name.com REST API (no libcloud driver exists for name.com).

**Effect-locality tiers (B4/INV-EXT-03), not reversibility.** Any verb whose effect is observable
OUTSIDE this instance is ``confirm``-tier and is never agent-autonomous. Read/inventory verbs are
``direct``. The host RESOLVES a verb's tier and fails closed: an unclassified verb is ``confirm``.

Credentials come from environment variables (see each class's docstring for the exact names).
"""

from __future__ import annotations

import http.client
import json
import os
import socket
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from registrar.enums import Tier

try:
    from libcloud.compute.providers import get_driver as _lc_compute_driver
    from libcloud.compute.types import Provider as _LC_Compute
    _LIBCLOUD_COMPUTE = True
except ImportError:
    _LIBCLOUD_COMPUTE = False

try:
    from libcloud.dns.providers import get_driver as _lc_dns_driver
    from libcloud.dns.types import Provider as _LC_DNS, RecordType as _LC_RecordType
    _LIBCLOUD_DNS = True
except ImportError:
    _LIBCLOUD_DNS = False


@dataclass
class NodeInfo:
    provider_ref: str
    name: str
    ip: str | None = None
    state: str = "active"


class ComputeDriver(ABC):
    """Provision/track hosts.

    Read verbs (direct tier): ``list_nodes``, ``get_node``.
    Outward verbs (confirm tier, staged + human-approved): ``create_node``, ``destroy_node``,
    ``start_node``, ``stop_node``, ``restart_node``, ``upload_ssh_key``, ``set_firewall_rules``.
    """

    name = "null"
    VERBS: dict[str, Tier] = {
        "list_nodes": Tier.direct, "get_node": Tier.direct,
        "create_node": Tier.confirm, "destroy_node": Tier.confirm,
        "start_node": Tier.confirm, "stop_node": Tier.confirm, "restart_node": Tier.confirm,
        "upload_ssh_key": Tier.confirm, "set_firewall_rules": Tier.confirm,
    }

    @abstractmethod
    def list_nodes(self) -> list[NodeInfo]: ...
    @abstractmethod
    def create_node(self, *, name: str, size: str = "s-1vcpu-1gb", region: str = "nyc1",
                    ssh_key_ids: list[str] | None = None) -> NodeInfo: ...
    @abstractmethod
    def destroy_node(self, provider_ref: str) -> bool: ...
    @abstractmethod
    def start_node(self, provider_ref: str) -> NodeInfo: ...
    @abstractmethod
    def stop_node(self, provider_ref: str) -> NodeInfo: ...
    @abstractmethod
    def restart_node(self, provider_ref: str) -> NodeInfo: ...
    @abstractmethod
    def upload_ssh_key(self, *, name: str, public_key: str) -> dict: ...
    @abstractmethod
    def set_firewall_rules(self, provider_ref: str, *, rules: list[dict]) -> dict: ...


class DnsDriver(ABC):
    """Manage DNS. ``upsert_record`` is confirm-tier — a record write takes effect at third-party
    resolvers and is cached for its TTL, so its effect is outward (B4)."""

    name = "null"
    VERBS: dict[str, Tier] = {
        "list_records": Tier.direct,
        "upsert_record": Tier.confirm, "register_domain": Tier.confirm,
        "delete_record": Tier.confirm,
    }

    @abstractmethod
    def list_records(self, domain: str) -> list[dict]: ...
    @abstractmethod
    def upsert_record(self, domain: str, *, rtype: str, name: str, value: str, ttl: int = 300) -> dict: ...
    @abstractmethod
    def register_domain(self, name: str) -> dict: ...
    @abstractmethod
    def delete_record(self, domain: str, record_id: str) -> bool: ...


class SmsDriver(ABC):
    name = "null"
    VERBS: dict[str, Tier] = {"send_sms": Tier.confirm}

    @abstractmethod
    def send_sms(self, *, to: str, body: str) -> dict: ...


# ── null in-memory backends (no egress, no credentials) ─────────────────────────────────────────────

@dataclass
class NullCompute(ComputeDriver):
    name: str = field(default="null")
    _nodes: dict[str, NodeInfo] = field(default_factory=dict)
    _ssh_keys: dict[str, dict] = field(default_factory=dict)

    def list_nodes(self) -> list[NodeInfo]:
        return list(self._nodes.values())

    def create_node(self, *, name: str, size: str = "s-1vcpu-1gb", region: str = "nyc1",
                    ssh_key_ids: list[str] | None = None) -> NodeInfo:
        ref = f"null-{uuid.uuid4().hex[:12]}"
        info = NodeInfo(provider_ref=ref, name=name, ip="203.0.113.10", state="active")
        self._nodes[ref] = info
        return info

    def destroy_node(self, provider_ref: str) -> bool:
        return self._nodes.pop(provider_ref, None) is not None

    def start_node(self, provider_ref: str) -> NodeInfo:
        n = self._nodes.get(provider_ref)
        if n:
            n.state = "active"
        return n or NodeInfo(provider_ref, "unknown", state="not-found")

    def stop_node(self, provider_ref: str) -> NodeInfo:
        n = self._nodes.get(provider_ref)
        if n:
            n.state = "off"
        return n or NodeInfo(provider_ref, "unknown", state="not-found")

    def restart_node(self, provider_ref: str) -> NodeInfo:
        n = self._nodes.get(provider_ref)
        if n:
            n.state = "active"
        return n or NodeInfo(provider_ref, "unknown", state="not-found")

    def upload_ssh_key(self, *, name: str, public_key: str) -> dict:
        key_id = f"null-key-{uuid.uuid4().hex[:8]}"
        self._ssh_keys[key_id] = {"id": key_id, "name": name}
        return {"id": key_id, "name": name}

    def set_firewall_rules(self, provider_ref: str, *, rules: list[dict]) -> dict:
        return {"provider_ref": provider_ref, "rules_applied": len(rules)}


@dataclass
class NullDns(DnsDriver):
    name: str = field(default="null")
    _records: list[dict] = field(default_factory=list)

    def list_records(self, domain: str) -> list[dict]:
        return [r for r in self._records if r["domain"] == domain]

    def upsert_record(self, domain: str, *, rtype: str, name: str, value: str, ttl: int = 300) -> dict:
        rec = {"id": uuid.uuid4().hex[:8], "domain": domain, "rtype": rtype,
               "name": name, "value": value, "ttl": ttl}
        self._records = [r for r in self._records
                         if not (r["domain"] == domain and r["rtype"] == rtype and r["name"] == name)]
        self._records.append(rec)
        return rec

    def register_domain(self, name: str) -> dict:
        return {"domain": name, "registered": True}

    def delete_record(self, domain: str, record_id: str) -> bool:
        before = len(self._records)
        self._records = [r for r in self._records if r.get("id") != record_id]
        return len(self._records) < before


@dataclass
class NullSms(SmsDriver):
    name: str = field(default="null")
    sent: list[dict] = field(default_factory=list)

    def send_sms(self, *, to: str, body: str) -> dict:
        rec = {"to": to, "body": body, "status": "queued"}
        self.sent.append(rec)
        return rec


# ── Incus backend (Unix-socket REST, no extra deps) ──────────────────────────────────────────────────

class IncusCompute(ComputeDriver):
    """Incus VMs via the Incus REST API over a Unix socket.

    Env: REGISTRAR_INCUS_SOCKET (default /var/lib/incus/unix.socket)
         REGISTRAR_INCUS_PROJECT (default: default)
    Incus lets you practice/test without cloud costs.
    """

    name: str = "incus"

    def __init__(self):
        self._socket = os.environ.get("REGISTRAR_INCUS_SOCKET", "/var/lib/incus/unix.socket")
        self._project = os.environ.get("REGISTRAR_INCUS_PROJECT", "default")

    def _req(self, method: str, path: str, body: dict | None = None) -> dict:
        conn = http.client.HTTPConnection("localhost")
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.connect(self._socket)
        conn.sock = sock
        headers = {"Content-Type": "application/json"} if body is not None else {}
        qs = f"?project={self._project}" if "?" not in path else f"&project={self._project}"
        conn.request(method, path + qs, json.dumps(body) if body is not None else None, headers)
        resp = conn.getresponse()
        return json.loads(resp.read())

    def _wait_op(self, op_url: str) -> dict:
        return self._req("GET", f"{op_url}/wait")

    def list_nodes(self) -> list[NodeInfo]:
        data = self._req("GET", "/1.0/instances")
        refs = data.get("metadata", [])
        nodes = []
        for ref in refs:
            name = ref.rstrip("/").split("/")[-1]
            detail = self._req("GET", f"/1.0/instances/{name}").get("metadata", {})
            ip = None
            for iface in detail.get("state", {}).get("network", {}).values():
                for addr in iface.get("addresses", []):
                    if addr.get("family") == "inet" and addr.get("scope") == "global":
                        ip = addr["address"]
                        break
                if ip:
                    break
            state = detail.get("state", {}).get("status", "unknown").lower()
            nodes.append(NodeInfo(provider_ref=name, name=name, ip=ip, state=state))
        return nodes

    def create_node(self, *, name: str, size: str = "s-1vcpu-1gb", region: str = "nyc1",
                    ssh_key_ids: list[str] | None = None) -> NodeInfo:
        body = {"name": name, "source": {"type": "image", "alias": "ubuntu/22.04"}}
        resp = self._req("POST", "/1.0/instances", body)
        if resp.get("type") == "async":
            self._wait_op(resp["operation"])
        self._req("PUT", f"/1.0/instances/{name}/state", {"action": "start", "force": False})
        return NodeInfo(provider_ref=name, name=name, state="active")

    def destroy_node(self, provider_ref: str) -> bool:
        self._req("PUT", f"/1.0/instances/{provider_ref}/state", {"action": "stop", "force": True})
        resp = self._req("DELETE", f"/1.0/instances/{provider_ref}")
        return resp.get("status_code", 500) < 400

    def start_node(self, provider_ref: str) -> NodeInfo:
        self._req("PUT", f"/1.0/instances/{provider_ref}/state", {"action": "start", "force": False})
        return NodeInfo(provider_ref=provider_ref, name=provider_ref, state="active")

    def stop_node(self, provider_ref: str) -> NodeInfo:
        self._req("PUT", f"/1.0/instances/{provider_ref}/state", {"action": "stop", "force": False})
        return NodeInfo(provider_ref=provider_ref, name=provider_ref, state="off")

    def restart_node(self, provider_ref: str) -> NodeInfo:
        self._req("PUT", f"/1.0/instances/{provider_ref}/state", {"action": "restart", "force": False})
        return NodeInfo(provider_ref=provider_ref, name=provider_ref, state="active")

    def upload_ssh_key(self, *, name: str, public_key: str) -> dict:
        # Incus uses cloud-init or exec to inject keys; no native key upload API.
        # ponytail: key injection via exec at create time is the upgrade path.
        raise NotImplementedError("Incus SSH key injection requires cloud-init config at create_node time")

    def set_firewall_rules(self, provider_ref: str, *, rules: list[dict]) -> dict:
        # Incus uses iptables/nftables profiles; apply via profile ACLs.
        # ponytail: Incus ACL profiles are the upgrade path (PUT /1.0/network-acls/<name>).
        raise NotImplementedError("Incus firewall rules require Incus ACL profiles — not yet wired")


# ── libcloud-backed compute (DigitalOcean, Vultr, AWS, Azure) ────────────────────────────────────────

_LC_PROVIDER_MAP = {
    "digitalocean": "DIGITALOCEAN",
    "vultr": "VULTR",
    "aws": "EC2",
    "azure": "AZURE_ARM",
}

# Env var names per provider: REGISTRAR_<PROVIDER>_KEY / _SECRET / _REGION
# DigitalOcean: KEY=personal_access_token, SECRET unused
# Vultr: KEY=api_key, SECRET unused
# AWS: KEY=access_key_id, SECRET=secret_access_key, REGION=us-east-1
# Azure: KEY=tenant_id, SECRET=subscription_id (plus REGISTRAR_AZURE_APP_ID / _APP_SECRET)


class LibcloudCompute(ComputeDriver):
    """apache-libcloud compute driver (DigitalOcean, Vultr, AWS EC2, Azure ARM).

    Requires the ``providers`` extra (``pip install registrar[providers]``).
    Env: REGISTRAR_<PROVIDER>_KEY, REGISTRAR_<PROVIDER>_SECRET, REGISTRAR_<PROVIDER>_REGION
    """

    def __init__(self, provider: str):
        if not _LIBCLOUD_COMPUTE:
            raise RuntimeError("apache-libcloud not installed; pip install registrar[providers]")
        lc_name = _LC_PROVIDER_MAP.get(provider)
        if lc_name is None:
            raise ValueError(f"unsupported libcloud provider: {provider!r}")
        key = os.environ.get(f"REGISTRAR_{provider.upper()}_KEY", "")
        secret = os.environ.get(f"REGISTRAR_{provider.upper()}_SECRET", "")
        region = os.environ.get(f"REGISTRAR_{provider.upper()}_REGION", "nyc1")
        cls = _lc_compute_driver(getattr(_LC_Compute, lc_name))
        kw: dict = {}
        if provider == "aws":
            kw["region"] = region
        elif provider == "azure":
            kw["subscription_id"] = secret
            secret = os.environ.get("REGISTRAR_AZURE_APP_SECRET", "")
        self._driver = cls(key, secret, **kw)
        self.name = provider
        self._provider = provider

    def _by_ref(self, provider_ref: str):
        for n in self._driver.list_nodes():
            if n.id == provider_ref:
                return n
        raise ValueError(f"node {provider_ref!r} not found")

    def _to_info(self, n) -> NodeInfo:
        ip = n.public_ips[0] if n.public_ips else None
        state = str(n.state).lower() if hasattr(n.state, "__str__") else "unknown"
        return NodeInfo(provider_ref=n.id, name=n.name, ip=ip, state=state)

    def list_nodes(self) -> list[NodeInfo]:
        return [self._to_info(n) for n in self._driver.list_nodes()]

    def create_node(self, *, name: str, size: str = "s-1vcpu-1gb", region: str = "nyc1",
                    ssh_key_ids: list[str] | None = None) -> NodeInfo:
        sizes = {s.id: s for s in self._driver.list_sizes()}
        locations = {loc.id: loc for loc in self._driver.list_locations()}
        sz = sizes.get(size) or next(iter(sizes.values()))
        loc = locations.get(region) or next(iter(locations.values()))
        images = self._driver.list_images()
        # prefer ubuntu 22.04; fallback to first available
        img = next((i for i in images if "ubuntu" in i.name.lower() and "22" in i.name), None) or images[0]
        kw: dict = {}
        if ssh_key_ids:
            kw["ex_ssh_key_ids"] = ssh_key_ids
        node = self._driver.create_node(name=name, size=sz, image=img, location=loc, **kw)
        return self._to_info(node)

    def destroy_node(self, provider_ref: str) -> bool:
        return self._driver.destroy_node(self._by_ref(provider_ref))

    def start_node(self, provider_ref: str) -> NodeInfo:
        node = self._by_ref(provider_ref)
        self._driver.start_node(node)
        return self._to_info(node)

    def stop_node(self, provider_ref: str) -> NodeInfo:
        node = self._by_ref(provider_ref)
        self._driver.stop_node(node)
        return self._to_info(node)

    def restart_node(self, provider_ref: str) -> NodeInfo:
        node = self._by_ref(provider_ref)
        self._driver.reboot_node(node)
        return self._to_info(node)

    def upload_ssh_key(self, *, name: str, public_key: str) -> dict:
        # libcloud exposes ex_import_keypair_from_string on EC2; DO uses ex_create_ssh_key
        if hasattr(self._driver, "ex_create_ssh_key"):
            key = self._driver.ex_create_ssh_key(name=name, ssh_key_pub=public_key)
            return {"id": str(key.id), "name": key.name}
        if hasattr(self._driver, "ex_import_keypair_from_string"):
            key = self._driver.ex_import_keypair_from_string(name=name, key_material=public_key)
            return {"name": key.name}
        raise NotImplementedError(f"SSH key upload not supported for provider {self._provider!r}")

    def set_firewall_rules(self, provider_ref: str, *, rules: list[dict]) -> dict:
        # Each provider exposes different firewall APIs; DO uses ex_create_firewall, EC2 uses security groups.
        # ponytail: provider-specific firewall is the upgrade path; this surfaces the intent.
        raise NotImplementedError(
            f"set_firewall_rules not yet implemented for {self._provider!r}; "
            "apply rules manually via the provider console or CLI"
        )


# ── libcloud-backed DNS (DigitalOcean, GoDaddy, Namecheap) ──────────────────────────────────────────

_LC_DNS_MAP = {
    "digitalocean": "DIGITALOCEAN",
    "godaddy": "GODADDY",
    "namecheap": "NAMECHEAP",
}


class LibcloudDns(DnsDriver):
    """apache-libcloud DNS driver (DigitalOcean, GoDaddy, Namecheap).

    Requires the ``providers`` extra.
    Env: REGISTRAR_<PROVIDER>_KEY, REGISTRAR_<PROVIDER>_SECRET
    """

    def __init__(self, provider: str):
        if not _LIBCLOUD_DNS:
            raise RuntimeError("apache-libcloud not installed; pip install registrar[providers]")
        lc_name = _LC_DNS_MAP.get(provider)
        if lc_name is None:
            raise ValueError(f"unsupported libcloud DNS provider: {provider!r}")
        key = os.environ.get(f"REGISTRAR_{provider.upper()}_KEY", "")
        secret = os.environ.get(f"REGISTRAR_{provider.upper()}_SECRET", "")
        cls = _lc_dns_driver(getattr(_LC_DNS, lc_name))
        self._driver = cls(key, secret)
        self.name = provider

    def _zone(self, domain: str):
        for z in self._driver.list_zones():
            if z.domain.rstrip(".") == domain.rstrip("."):
                return z
        raise ValueError(f"zone {domain!r} not found in provider")

    def list_records(self, domain: str) -> list[dict]:
        zone = self._zone(domain)
        return [
            {"id": r.id, "rtype": r.type, "name": r.name, "value": r.data,
             "ttl": getattr(r, "ttl", 300)}
            for r in self._driver.list_records(zone)
        ]

    def upsert_record(self, domain: str, *, rtype: str, name: str, value: str, ttl: int = 300) -> dict:
        zone = self._zone(domain)
        existing = [r for r in self._driver.list_records(zone)
                    if r.name == name and str(r.type) == rtype]
        if existing:
            rec = self._driver.update_record(existing[0], name=name, type=rtype, data=value,
                                             extra={"ttl": ttl})
        else:
            rec = self._driver.create_record(name=name, zone=zone, type=rtype, data=value,
                                              extra={"ttl": ttl})
        return {"id": rec.id, "rtype": str(rec.type), "name": rec.name, "value": rec.data}

    def register_domain(self, name: str) -> dict:
        raise NotImplementedError("domain registration not supported via libcloud DNS driver")

    def delete_record(self, domain: str, record_id: str) -> bool:
        zone = self._zone(domain)
        for r in self._driver.list_records(zone):
            if r.id == record_id:
                return self._driver.delete_record(r)
        return False


# ── name.com REST DNS driver ─────────────────────────────────────────────────────────────────────────

class NameComDns(DnsDriver):
    """name.com DNS via their v4 REST API (https://www.name.com/api-docs).

    Env: REGISTRAR_NAMECOM_KEY (API token), REGISTRAR_NAMECOM_SECRET (username)
    """

    name: str = "namecom"
    _BASE = "api.name.com"

    def __init__(self):
        import base64
        username = os.environ.get("REGISTRAR_NAMECOM_SECRET", "")
        token = os.environ.get("REGISTRAR_NAMECOM_KEY", "")
        creds = f"{username}:{token}".encode()
        self._auth = "Basic " + base64.b64encode(creds).decode()

    def _req(self, method: str, path: str, body: dict | None = None) -> dict:
        conn = http.client.HTTPSConnection(self._BASE)
        headers = {"Authorization": self._auth, "Content-Type": "application/json"}
        conn.request(method, f"/v4/{path.lstrip('/')}", json.dumps(body) if body else None, headers)
        resp = conn.getresponse()
        return json.loads(resp.read())

    def list_records(self, domain: str) -> list[dict]:
        data = self._req("GET", f"domains/{domain}/records")
        return [
            {"id": str(r["id"]), "rtype": r["type"], "name": r["host"], "value": r["answer"],
             "ttl": r.get("ttl", 300)}
            for r in data.get("records", [])
        ]

    def upsert_record(self, domain: str, *, rtype: str, name: str, value: str, ttl: int = 300) -> dict:
        existing = [r for r in self.list_records(domain) if r["rtype"] == rtype and r["name"] == name]
        if existing:
            rec_id = existing[0]["id"]
            body = {"host": name, "type": rtype, "answer": value, "ttl": ttl}
            data = self._req("PUT", f"domains/{domain}/records/{rec_id}", body)
            return {"id": str(data.get("id", rec_id)), "rtype": rtype, "name": name, "value": value}
        body = {"host": name, "type": rtype, "answer": value, "ttl": ttl}
        data = self._req("POST", f"domains/{domain}/records", body)
        return {"id": str(data.get("id", "")), "rtype": rtype, "name": name, "value": value}

    def register_domain(self, name: str) -> dict:
        # name.com supports domain purchase via API but requires payment method on file.
        # ponytail: POST /v4/domains:purchase is the upgrade path; requires billing setup.
        raise NotImplementedError("domain registration requires billing configuration in name.com")

    def delete_record(self, domain: str, record_id: str) -> bool:
        resp = self._req("DELETE", f"domains/{domain}/records/{record_id}")
        return "error" not in resp


# ── registry ─────────────────────────────────────────────────────────────────────────────────────────

_COMPUTE: dict[str, type[ComputeDriver]] = {"null": NullCompute, "incus": IncusCompute}
_DNS: dict[str, type[DnsDriver]] = {"null": NullDns, "namecom": NameComDns}
_SMS: dict[str, type[SmsDriver]] = {"null": NullSms}

# Register libcloud-backed providers (present only when providers extra is installed).
for _p in ("digitalocean", "vultr", "aws", "azure"):
    _COMPUTE[_p] = lambda p=_p: LibcloudCompute(p)  # type: ignore[assignment]
for _p in ("godaddy", "namecheap"):
    _DNS[_p] = lambda p=_p: LibcloudDns(p)  # type: ignore[assignment]
_DNS["digitalocean"] = lambda: LibcloudDns("digitalocean")  # type: ignore[assignment]


def get_compute(provider: str = "null") -> ComputeDriver:
    fac = _COMPUTE.get(provider, NullCompute)
    return fac() if callable(fac) else fac()


def get_dns(provider: str = "null") -> DnsDriver:
    fac = _DNS.get(provider, NullDns)
    return fac() if callable(fac) else fac()


def get_sms(provider: str = "null") -> SmsDriver:
    fac = _SMS.get(provider, NullSms)
    return fac() if callable(fac) else fac()


# The host RESOLVES a verb's tier and FAILS CLOSED (INV-EXT-03): an unknown verb is confirm-tier.
_ALL_VERBS: dict[str, Tier] = {**ComputeDriver.VERBS, **DnsDriver.VERBS, **SmsDriver.VERBS}


def tier_of(verb: str) -> Tier:
    return _ALL_VERBS.get(verb, Tier.confirm)
