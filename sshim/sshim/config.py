"""Runtime configuration for a mounted sshim instance.

Held on ``app.extensions['sshim']`` by ``register()`` so views can reach the session factory and the
injected host capabilities. ``extras`` is the seam a host fills with its capability bundle
(``current_actor`` / ``audit`` / ``extension_setting`` / the PAT trio); standalone leaves it empty and
every accessor in ``deps.py`` degrades to its documented default.

**Phase 2 seam (not built).** When lotek#1747 lands the exclusive-container seam, the host will add
``exclusive_container_acquire`` / ``exclusive_container_release`` to ``extras``; ``deps.host_hook`` is
the single place that reads them (via ``deps.exclusive_container_acquire``). Phase 1 works entirely
without them and never calls them — a profile's rendezvous is operator-typed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class SshimConfig:
    session_factory: Any
    engine: Any
    instance_path: Path
    url_prefix: str = "/sshim"
    base_template: str = "sshim/base.html"
    extras: dict[str, Any] = field(default_factory=dict)
