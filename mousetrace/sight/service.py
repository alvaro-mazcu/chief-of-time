from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from PIL import ImageGrab, Image  # type: ignore
import pytesseract  # type: ignore
from openai import OpenAI

from ..config import Settings, get_openai_api_key
from ..database.db import Database


@dataclass(slots=True)
class SightConfig:
    db_path: Path
    out_dir: Path
    interval_sec: int = 300
    model: Optional[str] = None


SIGHT_SYSTEM_PROMPT = (
    "You are a productivity-oriented content analyzer. "
    "Given raw OCR text from the user's current screen, summarize concisely (max 2 lines) what the user is looking at. "
    "Focus on the apparent task context (e.g., coding, docs, social media, messaging). "
    "Conclude with a classification token of 'productive', 'neutral', or 'distracting' based on likely focus."
)


def _summarize_ocr_text(text: str, model: Optional[str]) -> tuple[str, str]:
    key = get_openai_api_key()
    client = OpenAI(api_key=key)
    mdl = model or Settings.from_env().openai_model
    # Trim overly long OCR text to keep costs bounded
    t = (text or "")[:20000]
    resp = client.chat.completions.create(
        model=mdl,
        messages=[
            {"role": "system", "content": SIGHT_SYSTEM_PROMPT},
            {"role": "user", "content": f"OCR TEXT:\n\n{t}"},
        ],
        temperature=0.2,
    )
    content = (resp.choices[0].message.content or "").strip()
    # Heuristic: look for the 3 labels
    verdict = "neutral"
    lc = content.lower()
    if "productive" in lc:
        verdict = "productive"
    if "distracting" in lc:
        verdict = "distracting"
    return content, verdict


def _capture(path: Path) -> None:
    img = ImageGrab.grab(all_screens=True)
    img.save(str(path), format="PNG")


def _ocr(path: Path) -> str:
    try:
        img = Image.open(str(path))
        return pytesseract.image_to_string(img)
    except Exception as e:
        return f"<ocr-error> {e}"


def run_sight(cfg: SightConfig) -> None:
    cfg.out_dir.mkdir(parents=True, exist_ok=True)
    # Ensure schema exists
    db = Database(cfg.db_path)
    db.init_schema()
    db.close()

    while True:
        ts = time.time()
        try:
            fname = time.strftime("%Y%m%d_%H%M%S.png", time.localtime(ts))
            fpath = cfg.out_dir / fname
            _capture(fpath)
            text = _ocr(fpath)
            summary, verdict = _summarize_ocr_text(text, cfg.model)
            db2 = Database(cfg.db_path)
            try:
                db2.insert_screenshot(ts=ts, path=str(fpath), ocr_text=text, summary=summary, verdict=verdict)
            finally:
                db2.close()
        except KeyboardInterrupt:
            print("Sight capture stopped by user.")
            break
        except Exception as e:
            print(f"[sight] error: {e}")

        time.sleep(int(cfg.interval_sec))
