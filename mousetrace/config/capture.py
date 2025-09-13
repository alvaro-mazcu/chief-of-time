from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True)
class CaptureConfig:
    db_path: Path
    poll_hz: int = 10            # app/window polling frequency
    move_hz: int = 30            # mouse move sampling frequency
    record_moves: bool = True    # set False to only record clicks/scrolls
    record_keys: bool = True     # record keyboard events for KPM
    log_level: str = "INFO"

    def validate(self) -> None:
        if self.poll_hz < 1:
            raise ValueError("poll_hz must be >= 1")
        if self.move_hz < 1:
            raise ValueError("move_hz must be >= 1")
