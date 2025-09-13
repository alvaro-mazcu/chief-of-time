from __future__ import annotations

import sqlite3
from pathlib import Path


def summary(db_path: Path) -> dict:
    conn = sqlite3.connect(db_path)
    try:
        total_clicks = conn.execute(
            "SELECT COUNT(*) FROM pointer_events WHERE kind IN ('click_down','click_up')"
        ).fetchone()[0]
        total_moves = conn.execute(
            "SELECT COUNT(*) FROM pointer_events WHERE kind='move'"
        ).fetchone()[0]
        total_switches = conn.execute(
            "SELECT COUNT(*) FROM switches"
        ).fetchone()[0]
        total_keydowns = conn.execute(
            "SELECT COUNT(*) FROM key_events WHERE kind='key_down'"
        ).fetchone()[0]

        # Compute overall keypresses per minute using session durations
        # Sum(end - start) across sessions; if ended_at is NULL, use current time
        total_seconds = conn.execute(
            "SELECT SUM(COALESCE(ended_at, strftime('%s','now')) - started_at) FROM sessions"
        ).fetchone()[0] or 0.0
        kpm_overall = (total_keydowns / (total_seconds / 60.0)) if total_seconds > 0 else 0.0

        # Also compute last-60-min window KPM
        kpm_1h = conn.execute(
            "SELECT COUNT(*) / 60.0 FROM key_events WHERE kind='key_down' AND ts >= strftime('%s','now') - 3600"
        ).fetchone()[0] or 0.0

        # Best KPM in any 60s window (rolling)
        best_row = conn.execute(
            """
            WITH kd AS (
                SELECT ts FROM key_events WHERE kind='key_down'
            )
            SELECT k1.ts AS start_ts,
                   (
                       SELECT COUNT(*) FROM kd k2
                       WHERE k2.ts BETWEEN k1.ts AND k1.ts + 60.0
                   ) AS cnt
            FROM kd k1
            ORDER BY cnt DESC, start_ts ASC
            LIMIT 1
            """
        ).fetchone()
        if best_row is not None:
            best_start = float(best_row[0])
            best_cnt = int(best_row[1])
            best_window = {
                "start_ts": best_start,
                "end_ts": round(best_start + 60.0, 6),
                "keypresses": best_cnt,
            }
            best_kpm = float(best_cnt)  # 60-second window -> per-minute rate equals count
        else:
            best_window = None
            best_kpm = 0.0
        top_apps = conn.execute(
            "SELECT app_name, clicks FROM vw_clicks_by_app ORDER BY clicks DESC LIMIT 10"
        ).fetchall()
        return {
            "clicks": total_clicks,
            "moves": total_moves,
            "switches": total_switches,
            "keypresses": total_keydowns,
            "kpm_overall": round(kpm_overall, 2),
            "kpm_last_60m": round(float(kpm_1h), 2),
            "best_kpm": round(float(best_kpm), 2),
            "best_kpm_window": best_window,
            "top_apps": [(r[0], r[1]) for r in top_apps],
        }
    finally:
        conn.close()

