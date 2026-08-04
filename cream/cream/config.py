"""Runtime configuration for a mounted CREAM instance.

Held on ``app.extensions['cream']`` by ``register()`` so views can reach the session factory, injected
host models/capabilities, and the base template. ``extras`` is the seam a host fills with its capability
bundle (``extras['host']`` / ``current_actor`` / ``can_write`` / ``host_findings``); standalone leaves it
empty and every accessor degrades safely.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class CreamConfig:
    session_factory: Any
    engine: Any
    instance_path: Path
    url_prefix: str = "/cream"
    base_template: str = "cream/base.html"
    # Injected host models when mounted (lotek's Client). None -> CREAM's own (standalone has none).
    client_model: Any | None = None
    extras: dict[str, Any] = field(default_factory=dict)
