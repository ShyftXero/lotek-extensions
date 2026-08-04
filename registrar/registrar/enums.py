"""Registrar enumerations."""

from __future__ import annotations

import enum


class ServerKind(enum.Enum):
    static = "static"        # owned / long-lived (e.g. C2 team servers)
    transient = "transient"  # ephemeral cloud VPS, per-assessment, reassignable


class ServerState(enum.Enum):
    planned = "planned"
    active = "active"
    missing = "missing"      # reconcile: the provider no longer reports it (flagged LOUDLY, not dropped)
    destroyed = "destroyed"


class Tier(enum.Enum):
    """Action-safety tier a driver verb declares (decision: tiered confirm)."""

    direct = "direct"    # reversible / internal: executes in one step
    confirm = "confirm"  # costly / irreversible / outward: needs an explicit confirm (agents can only STAGE)
