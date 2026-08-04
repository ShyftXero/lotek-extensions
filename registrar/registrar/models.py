"""Registrar data model — offensive infrastructure inventory, v2-native.

Every surrogate PK is a UUIDv7 (``UuidPk``). ``engagement_id`` is a **UUID soft reference** to a core
``Engagement`` (no cross-schema FK; Registrar may run standalone). No authorization data lives here —
tenancy is core's; ``engagement_id`` merely records which engagement a piece of infra belongs to.

**Attribution is denormalised on purpose** (provider/ref/ip/timestamps live on the row) so a record
outlives the provider it came from — mirrors the object-store attribution rule.

Tables are ``registrar_``-prefixed.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import DateTime, Enum, ForeignKey, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from registrar.db import Base, TimestampMixin, UuidPk
from registrar.enums import ServerKind, ServerState


class Server(Base, UuidPk, TimestampMixin):
    """A tracked host — static (owned) or transient (ephemeral cloud). Provisioned/destroyed through a
    ComputeDriver; the row survives the provider (denormalised attribution)."""

    __tablename__ = "registrar_servers"

    kind: Mapped[ServerKind] = mapped_column(Enum(ServerKind), default=ServerKind.transient, index=True)
    state: Mapped[ServerState] = mapped_column(Enum(ServerState), default=ServerState.planned, index=True)
    name: Mapped[str] = mapped_column(String(255), index=True)
    provider: Mapped[str] = mapped_column(String(64), default="null")  # driver backend that owns it
    provider_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)  # e.g. DO droplet id
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    role: Mapped[str | None] = mapped_column(String(120), nullable=True)  # e.g. "redirector", "c2"
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Soft ref to core (transient servers are engagement-bound; static ones float -> null).
    engagement_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True, index=True)
    destroyed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class Domain(Base, UuidPk, TimestampMixin):
    """A domain in inventory, optionally checked out to an engagement."""

    __tablename__ = "registrar_domains"

    name: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    provider: Mapped[str] = mapped_column(String(64), default="null")  # registrar backend
    registered: Mapped[bool] = mapped_column(default=False)
    # Checkout: which engagement currently holds this domain (soft ref; null = available).
    checked_out_to: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True, index=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    records: Mapped[list[DnsRecord]] = relationship(
        back_populates="domain", cascade="all, delete-orphan"
    )


class DnsRecord(Base, UuidPk, TimestampMixin):
    """A DNS record under a domain. Upserted through a DnsDriver (direct-tier)."""

    __tablename__ = "registrar_dns_records"

    domain_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("registrar_domains.id", ondelete="CASCADE"), index=True
    )
    rtype: Mapped[str] = mapped_column(String(16))  # A, AAAA, CNAME, TXT, ...
    name: Mapped[str] = mapped_column(String(255))   # subdomain / @
    value: Mapped[str] = mapped_column(String(512))
    ttl: Mapped[int] = mapped_column(default=300)

    domain: Mapped[Domain] = relationship(back_populates="records")


class StagedAction(Base, UuidPk):
    """A confirm-tier action awaiting a human approval (INV-EXT-02). Created by the API; it can ONLY be
    executed from the approve endpoint, by a DIFFERENT interactive user. ``args_json`` holds the real
    args (needed to execute) — it is this table's, never the audit's (the audit projection is redacted)."""

    __tablename__ = "registrar_staged_actions"

    verb: Mapped[str] = mapped_column(String(64), index=True)
    provider: Mapped[str] = mapped_column(String(64), default="null")
    args_json: Mapped[str] = mapped_column(Text, default="{}")
    engagement_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True, index=True)
    initiator_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True, index=True)
    # pending | executed | rejected
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(UTC))
    confirmed_by: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class AuditRecord(Base, UuidPk):
    """Append-only local audit of every privileged action (who, verb, provider, args, result).

    Registrar keeps its OWN trail for the MVP; the roadmap's owed follow-up is to append to core's
    ``audit_events`` via a ``HostAudit`` host-contract verb so there is one defensible trail. No UPDATE,
    no DELETE — rows are only ever inserted."""

    __tablename__ = "registrar_audit"

    at: Mapped[datetime] = mapped_column(DateTime, index=True)
    actor: Mapped[str | None] = mapped_column(String(120), nullable=True)
    verb: Mapped[str] = mapped_column(String(64), index=True)
    provider: Mapped[str | None] = mapped_column(String(64), nullable=True)
    tier: Mapped[str] = mapped_column(String(16))
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)  # args + result summary (no secrets)
    result: Mapped[str] = mapped_column(String(32))  # executed | staged | error
