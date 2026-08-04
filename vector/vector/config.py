"""Runtime configuration for a mounted Vector instance.

Held on ``app.extensions['vector']`` by ``register()`` so blueprint views can reach the session factory,
injected host models, and the base template to extend. ``extras`` is the injection seam a host fills with
its capability bundle (``extras['host']`` / ``current_actor`` / ``can_write``); standalone leaves it empty
and every accessor degrades safely.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class VectorConfig:
    session_factory: Any
    engine: Any
    instance_path: Path
    url_prefix: str = "/vector"
    base_template: str = "vector/base.html"
    # Injected host models when mounted (e.g. lotek's Client). None -> use Vector's own.
    client_model: Any | None = None
    extras: dict[str, Any] = field(default_factory=dict)
