from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

from dotenv import load_dotenv


# Load .env from current working directory or parents
load_dotenv()  # honors .env and .env.<environment> if present


@dataclass(frozen=True)
class Settings:
    openai_api_key: Optional[str] = None
    openai_model: str = "gpt-4o-mini"

    @staticmethod
    def from_env() -> "Settings":
        return Settings(
            openai_api_key=os.getenv("OPENAI_API_KEY") or None,
            openai_model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
        )


def get_openai_api_key(override: Optional[str] = None) -> str:
    if override:
        return override
    key = Settings.from_env().openai_api_key
    if not key:
        raise RuntimeError(
            "OPENAI_API_KEY not configured. Create a .env file with OPENAI_API_KEY=... or set the env var."
        )
    return key
