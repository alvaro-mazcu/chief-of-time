from __future__ import annotations

import os
from pathlib import Path
from typing import Optional, List, Dict

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel

from ..agent import AgentConfig, AgentRunner
from ..config import get_openai_api_key
from ..analysis import summary as db_summary
from ..database.db import Database


class InsightRequest(BaseModel):
    question: str
    model: Optional[str] = None
    api_key: Optional[str] = None  # optional override


class InsightResponse(BaseModel):
    answer: str
    used_tools: List[Dict]


def create_app(db_path: Path) -> FastAPI:
    app = FastAPI(title="MouseTrace Insights API", version="0.1.0")
    # packaged schema lives under mousetrace/database/schema.sql
    schema_path = Path(__file__).resolve().parent.parent / "database" / "schema.sql"

    def get_runner(x_openai_key: Optional[str] = Header(default=None)) -> AgentRunner:
        try:
            key = get_openai_api_key(x_openai_key)
        except RuntimeError as e:
            raise HTTPException(status_code=503, detail=str(e))
        cfg = AgentConfig(db_path=db_path, schema_path=schema_path)
        return AgentRunner(cfg, api_key=key)

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    @app.get("/summary")
    def summary() -> dict:
        return db_summary(db_path)

    @app.get("/schema")
    def schema() -> dict:
        try:
            text = schema_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            text = ""
        return {"schema": text}

    @app.post("/insights", response_model=InsightResponse)
    def insights(req: InsightRequest, runner: AgentRunner = Depends(get_runner)) -> InsightResponse:
        if req.api_key:
            # recreate with explicit key override
            runner = AgentRunner(AgentConfig(db_path=db_path, schema_path=schema_path), api_key=req.api_key)
        result = runner.ask(req.question, model=req.model)
        return InsightResponse(**result)

    class Assessment(BaseModel):
        id: int
        start_ts: float
        end_ts: float
        verdict: str
        score: Optional[float] = None
        reason: Optional[str] = None
        created_at: float

    @app.get("/assessments", response_model=List[Assessment])
    def list_assessments(limit: int = 50) -> List[Assessment]:
        n = max(1, min(limit, 500))
        db = Database(db_path)
        try:
            rows = db._conn.execute(
                """
                SELECT id, start_ts, end_ts, verdict, score, reason, created_at
                FROM productivity_assessments
                ORDER BY start_ts DESC
                LIMIT ?
                """,
                (n,),
            ).fetchall()
            return [
                Assessment(
                    id=int(r[0]),
                    start_ts=float(r[1]),
                    end_ts=float(r[2]),
                    verdict=str(r[3]),
                    score=(float(r[4]) if r[4] is not None else None),
                    reason=(str(r[5]) if r[5] is not None else None),
                    created_at=float(r[6]),
                )
                for r in rows
            ]
        finally:
            db.close()

    class Screenshot(BaseModel):
        id: int
        ts: float
        path: str
        summary: Optional[str] = None
        verdict: Optional[str] = None
        created_at: float

    @app.get("/screenshots", response_model=List[Screenshot])
    def list_screenshots(limit: int = 50) -> List[Screenshot]:
        n = max(1, min(limit, 200))
        db = Database(db_path)
        try:
            rows = db._conn.execute(
                """
                SELECT id, ts, path, summary, verdict, created_at
                FROM screenshots
                ORDER BY ts DESC
                LIMIT ?
                """,
                (n,),
            ).fetchall()
            return [
                Screenshot(
                    id=int(r[0]),
                    ts=float(r[1]),
                    path=str(r[2]),
                    summary=(str(r[3]) if r[3] is not None else None),
                    verdict=(str(r[4]) if r[4] is not None else None),
                    created_at=float(r[5]),
                )
                for r in rows
            ]
        finally:
            db.close()

    @app.get("/screenshots/{sid}")
    def get_screenshot(sid: int) -> dict:
        db = Database(db_path)
        try:
            row = db._conn.execute(
                "SELECT id, ts, path, ocr_text, summary, verdict, created_at FROM screenshots WHERE id=?",
                (sid,),
            ).fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Not found")
            return {
                "id": int(row[0]),
                "ts": float(row[1]),
                "path": str(row[2]),
                "ocr_text": row[3],
                "summary": row[4],
                "verdict": row[5],
                "created_at": float(row[6]),
            }
        finally:
            db.close()

    return app


# Uvicorn reload/workers require an importable factory with no args.
# The CLI sets MOUSETRACE_DB_PATH before running with --reload.
def app_factory() -> FastAPI:  # pragma: no cover
    db_env = os.getenv("MOUSETRACE_DB_PATH")
    if not db_env:
        raise RuntimeError("MOUSETRACE_DB_PATH is not set; cannot create app")
    return create_app(Path(db_env).expanduser())
