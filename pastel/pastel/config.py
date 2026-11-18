"""Runtime configuration for a mounted Pastel instance.

Held on ``app.extensions['pastel']`` by ``register()`` so views can reach the session factory and the
injected host capabilities. ``extras`` is the seam a host fills with its capability bundle
(``current_actor`` / ``can_write`` / ``audit`` / the PAT trio); standalone leaves it empty and every
accessor in ``deps.py`` degrades to its documented default.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class PastelConfig:
    session_factory: Any
    engine: Any
    instance_path: Path
    url_prefix: str = "/pastel"
    base_template: str = "pastel/base.html"
    extras: dict[str, Any] = field(default_factory=dict)
