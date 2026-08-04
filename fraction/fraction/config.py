"""Runtime configuration for a mounted Fraction instance.

Held on ``app.extensions['fraction']`` by ``register()`` so blueprint views can reach the session
factory, artifact storage root, injected host models, and the base template to extend.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class FractionConfig:
    session_factory: Any
    engine: Any
    instance_path: Path
    url_prefix: str = "/fraction"
    base_template: str = "fraction/base.html"
    # Injected host models when mounted (e.g. Lotek's Client / Asset). None -> use Fraction's own.
    client_model: Any | None = None
    asset_model: Any | None = None
    severity_enum: Any | None = None
    extras: dict[str, Any] = field(default_factory=dict)

    @property
    def artifact_root(self) -> Path:
        return self.instance_path / "artifacts"
