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
    }
    tool_specs = [spec for _, spec in tools.values()]
    return tools, tool_specs

