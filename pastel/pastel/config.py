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
