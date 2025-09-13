from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Callable, Dict, List, Tuple

from ..analysis import summary as db_summary


def _ensure_select(sql: str) -> None:
    s = sql.strip().lower()
    if not s.startswith("select") and not s.startswith("with "):
        raise ValueError("Only read-only SELECT queries are allowed")
    forbidden = ("insert", "update", "delete", "drop", "alter", "create table", "attach", "vacuum", "pragma")
    if any(tok in s for tok in forbidden):
        raise ValueError("Query contains forbidden statements")


def tool_sql_query(db_path: Path, sql: str, limit: int = 200) -> dict:
    _ensure_select(sql)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.execute(sql)
        cols = [d[0] for d in cur.description]
        rows = cur.fetchmany(limit)
        data = [dict(zip(cols, r)) for r in rows]
        more = cur.fetchone() is not None
        return {"columns": cols, "rows": data, "truncated": more}
    finally:
        conn.close()


def tool_schema_text(schema_path: Path) -> str:
    try:
        return schema_path.read_text(encoding="utf-8")[:20000]
    except FileNotFoundError:
        return ""


def tool_summary(db_path: Path) -> dict:
    return db_summary(db_path)


def tool_top_apps(db_path: Path, metric: str = "clicks", limit: int = 10) -> List[Tuple[str, int]]:
    conn = sqlite3.connect(db_path)
    try:
        if metric == "moves":
            sql = "SELECT app_name, moves FROM vw_moves_share_by_app ORDER BY moves DESC LIMIT ?"
        else:
            sql = "SELECT app_name, clicks FROM vw_clicks_by_app ORDER BY clicks DESC LIMIT ?"
        rows = conn.execute(sql, (limit,)).fetchall()
        return [(r[0], int(r[1])) for r in rows]
    finally:
        conn.close()


def tool_sight_stats(db_path: Path, seconds: int = 600) -> dict:
    """Return counts, percentages, and a 0..1 score from screenshot verdicts in the last N seconds.

    Score heuristic: productive=1.0, neutral=0.5, distracting=0.0.
    """
    conn = sqlite3.connect(db_path)
    try:
        # Count verdicts over the time window
        rows = conn.execute(
            """
            SELECT verdict, COUNT(*) AS n
            FROM screenshots
            WHERE ts >= strftime('%s','now') - ?
            GROUP BY verdict
            """,
            (int(seconds),),
        ).fetchall()
        counts = { (r[0] or ""): int(r[1]) for r in rows }
        total = sum(counts.values())
        prod = counts.get("productive", 0)
        neut = counts.get("neutral", 0)
        dist = counts.get("distracting", 0)
        if total > 0:
            score = (prod * 1.0 + neut * 0.5 + dist * 0.0) / float(total)
            pct = {
                "productive": round(100.0 * prod / total, 2),
                "neutral": round(100.0 * neut / total, 2),
                "distracting": round(100.0 * dist / total, 2),
            }
        else:
            score = None
            pct = {"productive": 0.0, "neutral": 0.0, "distracting": 0.0}
        return {
            "window_seconds": int(seconds),
            "total": total,
            "counts": {"productive": prod, "neutral": neut, "distracting": dist},
            "percentages": pct,
            "score": (round(score, 3) if score is not None else None),
        }
    finally:
        conn.close()


def tool_assessment_stats(db_path: Path, seconds: int = 86400) -> dict:
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            """
            SELECT verdict, COUNT(*) AS n, AVG(COALESCE(score, NULL)) AS avg_score
            FROM productivity_assessments
            WHERE start_ts >= strftime('%s','now') - ?
            GROUP BY verdict
            """,
            (int(seconds),),
        ).fetchall()
        counts = { (r[0] or ""): int(r[1]) for r in rows }
        avg_scores = { (r[0] or ""): (float(r[2]) if r[2] is not None else None) for r in rows }
        total = sum(counts.values())
        pct = {k: (round(100.0 * v / total, 2) if total else 0.0) for k, v in counts.items()}
        # Overall score = weighted average of row scores if present; fallback heuristic like sight_stats
        overall_score = None
        if total:
            # Heuristic fallback when avg_score missing: good=1, neutral=0.5, bad=0
            score_map = {"good": 1.0, "neutral": 0.5, "bad": 0.0}
            num = 0.0
            for verdict, n in counts.items():
                s = avg_scores.get(verdict)
                if s is None:
                    s = score_map.get(verdict, 0.5)
                num += float(s) * n
            overall_score = round(num / total, 3)
        return {
            "window_seconds": int(seconds),
            "total": total,
            "counts": counts,
            "percentages": pct,
            "avg_scores": avg_scores,
            "score": overall_score,
        }
    finally:
        conn.close()


def tool_sleep_latest(db_path: Path) -> dict:
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT ts, duration_sec, score FROM sleep_logs ORDER BY ts DESC LIMIT 1"
        ).fetchone()
        if not row:
            return {"latest": None}
        return {
            "latest": {
                "ts": float(row[0]),
                "duration_sec": float(row[1]),
                "score": (float(row[2]) if row[2] is not None else None),
            }
        }
    finally:
        conn.close()


def tool_activity_stats(db_path: Path, seconds: int = 86400) -> dict:
    conn = sqlite3.connect(db_path)
    try:
        # Totals
        row = conn.execute(
            """
            SELECT COUNT(*) AS sessions, COALESCE(SUM(duration_sec),0.0) AS duration_sec, COALESCE(SUM(kcal),0.0) AS kcal
            FROM activity_logs
            WHERE ts >= strftime('%s','now') - ?
            """,
            (int(seconds),),
        ).fetchone()
        sessions = int(row[0]) if row else 0
        duration_sec = float(row[1]) if row else 0.0
        kcal = float(row[2]) if row else 0.0
        # By kind
        by_kind = conn.execute(
            """
            SELECT kind, COUNT(*) AS n, COALESCE(SUM(duration_sec),0.0) AS duration_sec, COALESCE(SUM(kcal),0.0) AS kcal
            FROM activity_logs
            WHERE ts >= strftime('%s','now') - ?
            GROUP BY kind
            ORDER BY n DESC
            """,
            (int(seconds),),
        ).fetchall()
        kinds = [
            {"kind": r[0], "sessions": int(r[1]), "duration_sec": float(r[2]), "kcal": float(r[3])}
            for r in by_kind
        ]
        # By intensity
        by_int = conn.execute(
            """
            SELECT COALESCE(intensity,'unknown') AS intensity, COUNT(*) AS n, COALESCE(SUM(duration_sec),0.0) AS duration_sec
            FROM activity_logs
            WHERE ts >= strftime('%s','now') - ?
            GROUP BY intensity
            ORDER BY n DESC
            """,
            (int(seconds),),
        ).fetchall()
        intensities = [
            {"intensity": r[0], "sessions": int(r[1]), "duration_sec": float(r[2])}
            for r in by_int
        ]
        return {
            "window_seconds": int(seconds),
            "sessions": sessions,
            "duration_sec": duration_sec,
            "kcal": kcal,
            "by_kind": kinds,
            "by_intensity": intensities,
        }
    finally:
        conn.close()


ToolFunc = Callable[..., Any]


def build_tools(db_path: Path, schema_path: Path) -> Tuple[Dict[str, Tuple[ToolFunc, dict]], List[dict]]:
    tools: Dict[str, Tuple[ToolFunc, dict]] = {
        "sql_query": (
            lambda sql, limit=200: tool_sql_query(db_path, sql=sql, limit=limit),
            {
                "type": "function",
                "function": {
                    "name": "sql_query",
                    "description": "Run a read-only SQL SELECT query against the telemetry database.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "sql": {"type": "string", "description": "A SELECT SQL statement."},
                            "limit": {"type": "integer", "minimum": 1, "maximum": 10000, "default": 200},
                        },
                        "required": ["sql"],
                    },
                },
            },
        ),
        "schema_text": (
            lambda: tool_schema_text(schema_path),
            {
                "type": "function",
                "function": {
                    "name": "schema_text",
                    "description": "Return the SQL schema DDL text for context.",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
        ),
        "summary": (
            lambda: tool_summary(db_path),
            {
                "type": "function",
                "function": {
                    "name": "summary",
                    "description": "Return high-level summary stats (clicks, moves, switches, keypresses, KPM).",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
        ),
        "top_apps": (
            lambda metric="clicks", limit=10: tool_top_apps(db_path, metric=metric, limit=limit),
            {
                "type": "function",
                "function": {
                    "name": "top_apps",
                    "description": "Top applications by clicks or moves.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "metric": {"type": "string", "enum": ["clicks", "moves"], "default": "clicks"},
                            "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 10},
                        },
                    },
                },
            },
        ),
        "sight_stats": (
            lambda seconds=600: tool_sight_stats(db_path, seconds=seconds),
            {
                "type": "function",
                "function": {
                    "name": "sight_stats",
                    "description": "Screenshot verdict mix (productive/neutral/distracting) and 0..1 score over last N seconds.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "seconds": {"type": "integer", "minimum": 60, "maximum": 86400, "default": 600},
                        },
                    },
                },
            },
        ),
        "assessment_stats": (
            lambda seconds=86400: tool_assessment_stats(db_path, seconds=seconds),
            {
                "type": "function",
                "function": {
                    "name": "assessment_stats",
                    "description": "Counts/percentages and overall score from productivity_assessments in the last N seconds.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "seconds": {"type": "integer", "minimum": 60, "maximum": 604800, "default": 86400},
                        },
                    },
                },
            },
        ),
        "sleep_latest": (
            lambda: tool_sleep_latest(db_path),
            {
                "type": "function",
                "function": {
                    "name": "sleep_latest",
                    "description": "Return the most recent sleep log with duration and score, if any.",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
        ),
        "activity_stats": (
            lambda seconds=86400: tool_activity_stats(db_path, seconds=seconds),
            {
                "type": "function",
                "function": {
                    "name": "activity_stats",
                    "description": "Aggregate physical activity over the last N seconds (sessions, duration, kcal, breakdowns).",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "seconds": {"type": "integer", "minimum": 60, "maximum": 604800, "default": 86400},
                        },
                    },
                },
            },
        ),
    }
    tool_specs = [spec for _, spec in tools.values()]
    return tools, tool_specs
