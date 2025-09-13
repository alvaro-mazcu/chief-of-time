from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass
class AgentConfig:
    db_path: Path
    schema_path: Path
    model: str = "gpt-4o-mini"

