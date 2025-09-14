from __future__ import annotations

import os
from pathlib import Path
from typing import Optional, List, Dict, Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request, File, UploadFile
from fastapi import status as http_status
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

    # ---- Audio upload and availability ----
    @app.post("/upload-audio", status_code=http_status.HTTP_201_CREATED)
    async def upload_audio(file: UploadFile = File(...)) -> dict:
        import time as _time
        import uuid
        from pathlib import Path as _Path

        if file is None:
            raise HTTPException(status_code=http_status.HTTP_400_BAD_REQUEST, detail="Missing file")

        allowed = {
            "audio/webm": "webm",
            "audio/ogg": "ogg",
            "audio/mpeg": "mp3",
        }
        mime = (file.content_type or "").lower()
        if mime not in allowed:
            raise HTTPException(status_code=http_status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail=f"Unsupported type: {mime}")

        # Storage layout
        base_dir = _Path.cwd() / "uploads" / "audio"
        base_dir.mkdir(parents=True, exist_ok=True)
        ts = int(_time.time())
        ext = allowed[mime]
        fname = f"voice-note-{ts}-{uuid.uuid4().hex[:8]}.{ext}"
        dest = base_dir / fname

        # Stream to disk with size check (25 MB)
        max_bytes = 25 * 1024 * 1024
        total = 0
        try:
            with dest.open("wb") as out:
                while True:
                    chunk = await file.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > max_bytes:
                        try:
                            out.close()
                            dest.unlink(missing_ok=True)
                        except Exception:
                            pass
                        raise HTTPException(status_code=http_status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="File too large (max 25MB)")
                    out.write(chunk)
        except HTTPException:
            raise
        except Exception as e:
            try:
                dest.unlink(missing_ok=True)
            except Exception:
                pass
            raise HTTPException(status_code=http_status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Storage error: {e}")

        rel_path = str(_Path("uploads") / "audio" / fname)
        return {
            "path": rel_path,
            "filename": fname,
            "mime_type": mime,
            "size_bytes": total,
        }

    class AudioAvailableRequest(BaseModel):
        path: str
        context: Optional[str] = None
        plan_date: Optional[str] = None
        user_id: Optional[str] = None

    @app.post("/audio-available", status_code=http_status.HTTP_202_ACCEPTED)
    async def audio_available(request: Request, x_openai_key: Optional[str] = Header(default=None)) -> dict:
        """Transcribe the uploaded audio with Whisper and convert to a daily plan, storing it.

        Accepts flexible JSON or form payloads to avoid 422s from strict validation.
        """
        import json as _json
        import time as _time
        from datetime import datetime
        from pathlib import Path as _Path
        from openai import OpenAI

        # Parse body robustly (JSON or form)
        payload: dict
        try:
            payload = await request.json()
            if not isinstance(payload, dict):
                raise ValueError("Body must be a JSON object")
        except Exception:
            try:
                form = await request.form()
                payload = {k: form.get(k) for k in ("path", "context", "plan_date", "user_id")}
            except Exception:
                payload = {}

        path_val = payload.get("path")
        if not path_val:
            raise HTTPException(status_code=http_status.HTTP_400_BAD_REQUEST, detail="Field 'path' is required")

        audio_path = _Path(path_val)
        if not audio_path.is_absolute():
            audio_path = _Path.cwd() / audio_path
        if not audio_path.exists():
            raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail="File not found at path")

        # OpenAI client
        try:
            key = get_openai_api_key(x_openai_key)
        except RuntimeError as e:
            raise HTTPException(status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(e))
        client = OpenAI(api_key=key)

        # 1) Transcribe with Whisper
        try:
            with audio_path.open("rb") as f:
                tr = client.audio.transcriptions.create(model="whisper-1", file=f)
            transcript = (tr.text or "").strip()
        except Exception as e:
            raise HTTPException(status_code=http_status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Transcription failed: {e}")

        # 2) Convert transcript to daily plan JSON via LLM
        PLAN_SYSTEM = (
            "You convert spoken notes into a daily plan as JSON. "
            "Return ONLY a JSON array named implicitly (no key) with todo items. "
            "Each item is an object: {id: string, title: string, completed: boolean, priority: one of 'high','medium','low'}. "
            "Infer sensible priorities from wording; default completed=false. No extra text."
        )
        try:
            comp = client.chat.completions.create(
                model=AgentConfig(db_path=db_path, schema_path=schema_path).model,
                messages=[
                    {"role": "system", "content": PLAN_SYSTEM},
                    {"role": "user", "content": transcript},
                ],
                temperature=0.2,
            )
            raw = (comp.choices[0].message.content or "").strip()
            # Strip code fences if present
            content = raw
            if content.startswith("```"):
                # Remove leading fence line and trailing fence
                parts = content.split("\n", 1)
                content = parts[1] if len(parts) > 1 else content
                if content.endswith("```"):
                    content = content.rsplit("```", 1)[0]
                content = content.strip()
            # If still not clean, try to extract JSON array substring
            def _extract_json_array(txt: str) -> str:
                l = txt.find("[")
                r = txt.rfind("]")
                return txt[l:r+1] if l != -1 and r != -1 and r > l else txt
            content = _extract_json_array(content)
            # Parse and coerce into a normalized list of items
            data = _json.loads(content)
            if isinstance(data, dict):
                items = [data]
            elif isinstance(data, list):
                items = data
            else:
                items = [str(data)]

            # Normalize items
            import uuid as _uuid
            norm: list = []
            for idx, itm in enumerate(items):
                if isinstance(itm, str):
                    obj = {"id": _uuid.uuid4().hex[:8], "title": itm, "completed": False, "priority": "medium"}
                elif isinstance(itm, dict):
                    obj = dict(itm)
                    obj.setdefault("id", _uuid.uuid4().hex[:8])
                    obj.setdefault("title", obj.get("task") or obj.get("name") or "Untitled")
                    comp_val = obj.get("completed", False)
                    if isinstance(comp_val, str):
                        obj["completed"] = comp_val.lower() in ("true", "yes", "done")
                    else:
                        obj["completed"] = bool(comp_val)
                    pr = str(obj.get("priority", "medium")).lower()
                    if pr not in ("high", "medium", "low"):
                        pr = "medium"
                    obj["priority"] = pr
                else:
                    obj = {"id": _uuid.uuid4().hex[:8], "title": str(itm), "completed": False, "priority": "medium"}
                norm.append({
                    "id": str(obj.get("id")),
                    "title": str(obj.get("title")),
                    "completed": bool(obj.get("completed", False)),
                    "priority": str(obj.get("priority", "medium")).lower(),
                })
            plan_obj = norm
        except Exception as e:
            raise HTTPException(status_code=http_status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Plan synthesis failed: {e}")

        # 3) Store in daily_plans
        plan_date = (payload.get("plan_date") or datetime.now().strftime("%Y-%m-%d"))
        db = Database(db_path)
        try:
            db.init_schema()
            payload_json = _json.dumps(plan_obj, ensure_ascii=False)
            row_id = db.insert_daily_plan(plan_date=plan_date, plan_json=payload_json)
            row = db.get_daily_plan(plan_date=plan_date)
            # Validate round-trip
            if not row or row.get("plan_json") != payload_json:
                raise RuntimeError("Plan row not persisted correctly")
        except Exception as e:
            db.close()
            raise HTTPException(status_code=http_status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"DB insert failed: {e}")
        finally:
            try:
                db.close()
            except Exception:
                pass

        return {
            "id": row_id,
            "status": "processed",
            "path": str(path_val),
            "plan_date": plan_date,
            "plan": plan_obj,
            "transcript_preview": transcript[:500],
            "received_at": _time.time(),
        }

    return app

# Uvicorn reload/workers require an importable factory with no args.
# The CLI sets MOUSETRACE_DB_PATH before running with --reload.
def app_factory() -> FastAPI:  # pragma: no cover
    db_env = os.getenv("MOUSETRACE_DB_PATH")
    if not db_env:
        raise RuntimeError("MOUSETRACE_DB_PATH is not set; cannot create app")
    return create_app(Path(db_env).expanduser())
