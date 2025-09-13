from __future__ import annotations

import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

try:
    # External dependency; ensure it's available in environment
    from mac_notifications import client as mac_client  # type: ignore
except Exception:  # pragma: no cover - runtime fallback
    mac_client = None  # type: ignore

from ..agent.config import AgentConfig
from ..agent.runner import AgentRunner
from ..database.db import Database


@dataclass(slots=True)
class NotifierConfig:
    db_path: Path
    schema_path: Path
    interval_sec: int = 120
    icon_path: Optional[str] = None
    model: Optional[str] = None


def _parse_verdict_and_score(text: str) -> tuple[str, Optional[float]]:
    """Parse agent free-form text into verdict in {good, neutral, bad} and optional score.

    Heuristics:
    - Look for `score=<float>` first.
    - Otherwise, infer from keywords.
    """
    t = (text or "").lower()

    m = re.search(r"score\s*[=:]\s*([01](?:\.\d+)?)", t)
    score = None
    if m:
        try:
            score = float(m.group(1))
        except Exception:
            score = None

    print(score)

    verdict = "neutral"
    if score is not None:
        if score >= 0.66:
            verdict = "good"
        elif score < 0.33:
            verdict = "bad"
        else:
            verdict = "neutral"
        return verdict, score

    # Fallback keyword detection
    positives = ("very productive", "highly productive", "productive", "focused", "on task")
    negatives = ("not productive", "unproductive", "idle", "distracted", "low activity", "mostly browsing")
    if any(p in t for p in positives):
        verdict = "good"
    if any(n in t for n in negatives):
        verdict = "bad"
    return verdict, None


def _subtitle_for_verdict(verdict: str) -> str:
    v = verdict.lower().strip()
    if v == "good":
        return "🟢 YOU'RE DOING GOOD SO FAR 🟢"
    if v == "bad":
        return "🔴 DISTRACTED — GET BACK ON TRACK 🔴"
    return "🟡 OKAY — KEEP FOCUS 🟡"


def _sound_for_verdict(verdict: str) -> str:
    v = verdict.lower().strip()
    if v == "good":
        return "mario_happy"
    if v == "bad":
        return "mario_bad"
    return "mario_neutral"


def _send_notification(title: str, subtitle: str, sound: str = "default", icon_path: Optional[str] = None) -> None:
    if mac_client is None:
        # Fallback to stdout if mac_notifications is not installed
        print(f"[notify] {title} — {subtitle}")
        return
    kwargs = {
        "title": title,
        "subtitle": subtitle,
        "sound": sound,
    }
    if icon_path:
        kwargs["icon"] = icon_path
    mac_client.create_notification(**kwargs)


def run_notifier(cfg: NotifierConfig) -> None:
    """Run a loop that every `interval_sec` asks the Agent for a last-window verdict and notifies.

    Ctrl-C to exit.
    """
    agent_cfg = AgentConfig(db_path=cfg.db_path, schema_path=cfg.schema_path, model=cfg.model or "gpt-4o-mini")
    runner = AgentRunner(agent_cfg)

    interval = int(cfg.interval_sec)

    while True:
        try:
            # Frame the question to make the agent use a time filter and be concise.
            q = (
                "Assess productivity in the last {n} seconds. "
                "Prefer using the 'summary' and 'top_apps' tools; avoid raw SQL unless necessary. "
                "If you must query, call 'schema_text' first to see the exact table/view names. "
                "Respond briefly with a qualitative verdict (good/neutral/bad) and optionally include "
                "a numeric score in the form 'score=0.xx'."
            ).format(n=interval)

            window_end = time.time()
            res = runner.ask(q)
            answer = res.get("answer", "").strip()
            verdict, score = _parse_verdict_and_score(answer)
            subtitle = _subtitle_for_verdict(verdict)
            sound = _sound_for_verdict(verdict)
            _send_notification(title="Chief of Time says...", subtitle=subtitle, icon_path=cfg.icon_path, sound=sound)

            # Persist the assessment for the window
            window_start = window_end - float(interval)
            try:
                db = Database(cfg.db_path)
                db.insert_assessment(start_ts=window_start, end_ts=window_end, verdict=verdict, score=score, reason=answer)
                db.close()
            except Exception as e:
                print(f"[notify] Failed to persist assessment: {e}")
        except KeyboardInterrupt:
            print("Notifier stopped by user.")
            break
        except Exception as e:
            # Don't crash the loop on transient errors
            print(f"[notify] Error: {e}")

        # Sleep until next window
        time.sleep(interval)
