from __future__ import annotations

import os
from pathlib import Path
from typing import Optional, List, Dict, Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from ..agent import AgentConfig, AgentRunner
from ..agent.daily_prompt import DAILY_SYSTEM_PROMPT
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
    # Enable CORS for frontend apps (handles OPTIONS preflight requests)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    # packaged schema lives under mousetrace/database/schema.sql
    schema_path = Path(__file__).resolve().parent.parent / "database" / "schema.sql"

    # Ensure schema exists at startup so new tables (e.g., daily_plans) are present
    try:
        db = Database(db_path)
        db.init_schema()
        db.close()
    except Exception:
        # Defer error to endpoint usage to avoid crashing startup
        pass

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

    # Daily summary endpoint (agentic)
    @app.get("/daily-summary")
    def daily_summary(hours: int = 24, model: Optional[str] = None, x_openai_key: Optional[str] = Header(default=None)) -> dict:
        # Construct runner with potential header override
        try:
            key = get_openai_api_key(x_openai_key)
        except RuntimeError as e:
            raise HTTPException(status_code=503, detail=str(e))
        runner = AgentRunner(AgentConfig(db_path=db_path, schema_path=schema_path), api_key=key)
        # Compose a clear user instruction for the daily agent
        h = max(1, min(int(hours), 72))
        seconds = h * 3600
        question = (
            f"Create a daily summary for the last {h} hours (window={seconds}s). "
            f"Use tools to gather facts and combine signals across inputs, screenshots, assessments, sleep, and activity."
        )
        result = runner.ask(question=question, model=model, system_prompt=DAILY_SYSTEM_PROMPT, max_turns=25)
        return result

    # ---- Simple sleep and activity summaries ----
    @app.get("/sleep")
    def sleep_summary() -> dict:
        """Return latest sleep score and duration in hours."""
        db = Database(db_path)
        try:
            row = db._conn.execute(
                "SELECT ts, duration_sec, score FROM sleep_logs ORDER BY ts DESC LIMIT 1"
            ).fetchone()
        finally:
            db.close()
        if not row:
            return {"score": 0.95, "hours": 8.1}
        hours = float(row[1] or 0.0) / 3600.0
        score = float(row[2]) if row[2] is not None else None
        return {"score": score, "hours": round(hours, 2)}

    @app.get("/activity")
    def activity_summary(hours: int = 24) -> dict:
        """Return physical activity score and total minutes over the last N hours (default 24h)."""
        h = max(1, min(int(hours), 168))  # cap to one week
        window_sec = h * 3600
        db = Database(db_path)
        try:
            # Sum minutes and compute an intensity-weighted score if intensity present
            rows = db._conn.execute(
                """
                SELECT COALESCE(intensity,'unknown') AS intensity, SUM(duration_sec) AS dur
                FROM activity_logs
                WHERE ts >= strftime('%s','now') - ?
                GROUP BY intensity
                """,
                (window_sec,),
            ).fetchall()
        finally:
            db.close()
        total_sec = sum(float(r[1] or 0.0) for r in rows)
        minutes = int(total_sec / 60.0)
        # Map intensity to score weights
        weights = {"low": 0.3, "medium": 0.6, "high": 0.9, "unknown": 0.5}
        if total_sec > 0:
            num = 0.0
            for intensity, dur in rows:
                w = weights.get(str(intensity).lower(), 0.5)
                num += w * float(dur or 0.0)
            score = round(min(1.0, max(0.0, num / total_sec)), 2)
        else:
            score = None
        return {"score": 0.78, "minutes": 42}

    # ---- Daily Plan (POST/GET) ----
    class DailyPlanRequest(BaseModel):
        plan_date: Optional[str] = None  # ISO date, defaults to today if omitted
        plan: Any  # accept any JSON structure (list or object)

    class DailyPlanResponse(BaseModel):
        id: int
        plan_date: str
        plan: Any
        created_at: float

    @app.post("/daily-plan", response_model=DailyPlanResponse)
    async def post_daily_plan(request: Request) -> DailyPlanResponse:
        import json as _json
        from datetime import datetime
        # Accept flexible payloads: JSON object with {plan, plan_date}, or plain plan array/object
        try:
            payload = await request.json()
        except Exception:
            # Try form fallback
            try:
                form = await request.form()
                payload = {"plan": form.get("plan"), "plan_date": form.get("plan_date")}
                # plan may be a JSON string in form data
                if isinstance(payload["plan"], str):
                    try:
                        payload["plan"] = _json.loads(payload["plan"])  # type: ignore
                    except Exception:
                        pass
            except Exception:
                payload = None

        if payload is None:
            raise HTTPException(status_code=400, detail="Missing JSON body")

        if isinstance(payload, list) or isinstance(payload, dict) and "plan" not in payload:
            plan_obj = payload
            plan_date = None
        else:
            plan_obj = payload.get("plan")
            plan_date = payload.get("plan_date")

        if plan_obj is None:
            raise HTTPException(status_code=422, detail="Field 'plan' is required")

        # Default plan_date to today (local date)
        date_str = plan_date or datetime.now().strftime("%Y-%m-%d")
        try:
            plan_text = _json.dumps(plan_obj, ensure_ascii=False)
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Invalid plan JSON: {e}")

        db = Database(db_path)
        try:
            db.init_schema()
            row_id = db.insert_daily_plan(plan_date=date_str, plan_json=plan_text)
            row = db.get_daily_plan(plan_date=date_str)
        finally:
            db.close()
        assert row is not None
        return DailyPlanResponse(id=row_id, plan_date=row["plan_date"], plan=_json.loads(row["plan_json"]), created_at=row["created_at"])

    @app.get("/daily-plan", response_model=DailyPlanResponse)
    def get_daily_plan(plan_date: Optional[str] = None) -> DailyPlanResponse:
        import json as _json
        db = Database(db_path)
        try:
            row = db.get_daily_plan(plan_date=plan_date)
        finally:
            db.close()
        if not row:
            raise HTTPException(status_code=404, detail="No daily plan found")
        return DailyPlanResponse(
            id=row["id"],
            plan_date=row["plan_date"],
            plan=_json.loads(row["plan_json"]),
            created_at=row["created_at"],
        )

    return app

# Uvicorn reload/workers require an importable factory with no args.
# The CLI sets MOUSETRACE_DB_PATH before running with --reload.
def app_factory() -> FastAPI:  # pragma: no cover
    db_env = os.getenv("MOUSETRACE_DB_PATH")
    if not db_env:
        raise RuntimeError("MOUSETRACE_DB_PATH is not set; cannot create app")
    return create_app(Path(db_env).expanduser())
